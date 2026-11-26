#!/usr/bin/env python3
"""Historical, no-lookahead Gate.io Spot backtest for the 1m scalp strategy.

Example:
  python backtest.py --days 26 --pairs BTC_USDT,ETH_USDT,SOL_USDT,XRP_USDT
All entries are filled at the next candle open. Taker fees and adverse slippage
are charged on both sides. If a candle touches stop and target, stop is assumed
first (conservative). Each symbol is simulated independently.
"""
import argparse
import json
import math
import sys
import time
from bisect import bisect_right
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests

from indicators.pure_ta import target_index
from settings import DEFAULT_SETTINGS
from strategies.engine import (
    check_htf_15m_trend,
    check_htf_1h_trend,
    evaluate_ema_vwap_vol,
    evaluate_momentum,
)

BASE_URL = "https://api.gateio.ws/api/v4"
INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900}


def utc_text(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def fetch_history(
    pair: str,
    interval: str,
    start_ts: int,
    end_ts: int,
    session: Optional[requests.Session] = None,
) -> List[Dict[str, Any]]:
    """Download historical Gate.io candles in non-overlapping <=1000-bar chunks."""
    if interval not in INTERVAL_SECONDS:
        raise ValueError(f"Unsupported interval: {interval}")
    step = INTERVAL_SECONDS[interval]
    client = session or requests.Session()
    client.headers.update({"Accept": "application/json", "User-Agent": "GateioSpotProScalper-Backtest/1.0"})
    cursor = (start_ts // step) * step
    candles_by_ts: Dict[int, Dict[str, Any]] = {}

    while cursor < end_ts:
        chunk_end = min(end_ts, cursor + step * 800)
        params = {
            "currency_pair": pair,
            "interval": interval,
            "from": cursor,
            "to": chunk_end,
        }
        response = None
        for attempt in range(5):
            response = client.get(f"{BASE_URL}/spot/candlesticks", params=params, timeout=25)
            if response.status_code == 429 or response.status_code >= 500:
                time.sleep(min(2 ** attempt, 12))
                continue
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Gate.io candle API {response.status_code} for {pair}/{interval}: "
                    f"{response.text[:500]}"
                )
            break
        else:
            raise RuntimeError(f"Gate.io API retry limit reached for {pair} {interval}")
        raw = response.json()
        if not isinstance(raw, list):
            raise RuntimeError(f"Unexpected candle payload for {pair} {interval}: {raw!r}")
        # Reuse the application's exact public API candle parser.
        from core.gateio_api import GateioSpotAPI
        parsed = GateioSpotAPI.parse_candles(raw)
        for candle in parsed:
            stamp = int(candle["ts"])
            if stamp < start_ts or stamp + step > end_ts:
                continue
            if candle.get("window_closed") is False:
                continue
            candles_by_ts[stamp] = candle

        cursor = chunk_end + step
        time.sleep(0.08)

    return [candles_by_ts[stamp] for stamp in sorted(candles_by_ts)]


def _metrics(trades: List[Dict[str, Any]], initial_balance: float) -> Dict[str, Any]:
    count = len(trades)
    wins = [t for t in trades if t["net_pnl_usdt"] > 0]
    losses = [t for t in trades if t["net_pnl_usdt"] < 0]
    gross_profit = sum(t["net_pnl_usdt"] for t in wins)
    gross_loss = abs(sum(t["net_pnl_usdt"] for t in losses))
    balance = initial_balance
    peak = balance
    max_dd = 0.0
    for trade in sorted(trades, key=lambda item: item["exit_ts"]):
        balance += trade["net_pnl_usdt"]
        peak = max(peak, balance)
        if peak > 0:
            max_dd = max(max_dd, (peak - balance) / peak * 100.0)
    return {
        "trades": count,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": (len(wins) / count * 100.0) if count else 0.0,
        "net_pnl_usdt": sum(t["net_pnl_usdt"] for t in trades),
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0),
        "max_drawdown_pct": max_dd,
        "average_trade_usdt": (sum(t["net_pnl_usdt"] for t in trades) / count) if count else 0.0,
        "average_win_usdt": (gross_profit / len(wins)) if wins else 0.0,
        "average_loss_usdt": (-gross_loss / len(losses)) if losses else 0.0,
    }


def simulate_pair(
    pair: str,
    base: List[Dict[str, Any]],
    medium: List[Dict[str, Any]],
    long: List[Dict[str, Any]],
    period_start: int,
    test_start: int,
    notional: float,
    fee_pct: float,
    slippage_bps: float,
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    """Run the same closed-candle entry and ATR/time-exit rules without lookahead."""
    if len(base) < 250:
        raise RuntimeError(f"Insufficient 1m candles for {pair}: {len(base)}")
    fee = fee_pct / 100.0
    slip = slippage_bps / 10000.0
    max_hold = max(1, int(settings["trading"].get("max_holding_minutes", 15)))
    medium_step = INTERVAL_SECONDS[settings["long_timeframe"]]
    long_step = INTERVAL_SECONDS[settings["very_long_timeframe"]]
    medium_close_times = [int(c["ts"]) + medium_step for c in medium]
    long_close_times = [int(c["ts"]) + long_step for c in long]
    trades: List[Dict[str, Any]] = []
    position: Optional[Dict[str, Any]] = None
    last_entry_ts = -10**18
    warmup = 205

    for i in range(warmup, len(base) - 1):
        candle = base[i]
        candle_close_ts = int(candle["ts"]) + 60

        if position is not None:
            stop = float(position["stop"])
            target = float(position["target"])
            low = float(candle["low"])
            high = float(candle["high"])
            opened = float(candle["open"])
            exit_price = None
            reason = ""

            # Conservative intrabar ambiguity: if stop and target are both hit,
            # assume the stop was hit first.
            if low <= stop:
                exit_price = min(opened, stop) if opened < stop else stop
                reason = "SL" if not position["trailing"] else "TRAIL_SL"
            elif high >= target:
                exit_price = max(opened, target) if opened > target else target
                reason = "TP"

            if exit_price is None:
                # New high-water stop applies from the next 1m candle onward.
                position["highest"] = max(float(position["highest"]), high)
                gain_pct = (position["highest"] / position["entry"] - 1.0) * 100.0
                trade_cfg = settings["trading"]
                if gain_pct >= float(trade_cfg.get("break_even_activation_pct", 0.5)):
                    fee_slip_buffer = 2.0 * (fee_pct + slippage_bps / 100.0) / 100.0
                    position["stop"] = max(
                        float(position["stop"]),
                        float(position["entry"]) * (1.0 + fee_slip_buffer),
                    )
                if gain_pct >= float(trade_cfg.get("trailing_activation_pct", 0.75)):
                    position["trailing"] = True
                    trail = float(trade_cfg.get("trailing_distance_pct", 0.3)) / 100.0
                    position["stop"] = max(
                        float(position["stop"]),
                        float(position["highest"]) * (1.0 - trail),
                    )

                held_bars = i - int(position["entry_index"]) + 1
                if held_bars >= max_hold:
                    exit_price = float(candle["close"])
                    reason = "TIME_EXIT"

            if exit_price is not None:
                exit_fill = max(0.0, exit_price * (1.0 - slip))
                quantity = float(position["quantity"])
                net_proceeds = quantity * exit_fill * (1.0 - fee)
                net_cost = float(position["notional"]) * (1.0 + fee)
                pnl = net_proceeds - net_cost
                trades.append({
                    "symbol": pair,
                    "entry_ts": int(position["entry_ts"]),
                    "exit_ts": int(candle["ts"]) + 60,
                    "entry": float(position["entry"]),
                    "exit": exit_fill,
                    "reason": reason,
                    "net_pnl_usdt": pnl,
                    "net_pnl_pct": (pnl / net_cost * 100.0) if net_cost else 0.0,
                    "held_minutes": held_bars if reason == "TIME_EXIT" else i - int(position["entry_index"]) + 1,
                })
                position = None
            continue

        if candle_close_ts < period_start or candle_close_ts < 0:
            continue
        next_candle = base[i + 1]
        entry_ts = int(next_candle["ts"])
        if entry_ts < period_start or entry_ts - last_entry_ts < int(settings.get("signal_cooldown_min", 10)) * 60:
            continue

        # Only use higher-timeframe candles that had fully closed by this 1m close.
        mi = bisect_right(medium_close_times, candle_close_ts)
        li = bisect_right(long_close_times, candle_close_ts)
        medium_closed = medium[max(0, mi - 220):mi]
        long_closed = long[max(0, li - 220):li]
        if settings.get("use_htf_filter", True) and not check_htf_15m_trend(medium_closed, closed_only=False):
            continue
        if settings.get("use_very_long_filter", True) and not check_htf_1h_trend(long_closed, closed_only=False):
            continue

        window = base[max(0, i - int(settings["candle_limit"]) + 1):i + 1]
        vwap_window = medium[max(0, mi - 300):mi]
        signal = evaluate_ema_vwap_vol(window, settings, vwap_candles=vwap_window) if settings.get("enable_ema_vwap_vol", True) else {"signal": False}
        if settings.get("enable_momentum", False):
            momentum = evaluate_momentum(window, settings, vwap_candles=vwap_window)
            if not (signal.get("signal") and momentum.get("signal")):
                continue
        elif not signal.get("signal"):
            continue

        atr = float(signal.get("atr") or 0.0)
        reference = float(next_candle["open"])
        if atr <= 0 or reference <= 0:
            continue
        entry_fill = reference * (1.0 + slip)
        atr_pct = atr / entry_fill * 100.0
        trade_cfg = settings["trading"]
        stop_pct = max(
            float(trade_cfg.get("stop_loss_pct", 0.55)),
            atr_pct * float(trade_cfg.get("atr_stop_multiplier", 1.6)),
        )
        target_pct = max(
            float(trade_cfg.get("take_profit_pct", 0.65)),
            atr_pct * float(trade_cfg.get("atr_target_multiplier", 1.25)),
        )
        stop_price = entry_fill * (1.0 - stop_pct / 100.0)
        target_price = entry_fill * (1.0 + target_pct / 100.0)
        quantity = notional / entry_fill
        position = {
            "entry": entry_fill,
            "entry_ts": entry_ts,
            "entry_index": i + 1,
            "quantity": quantity,
            "notional": notional,
            "stop": stop_price,
            "target": target_price,
            "highest": entry_fill,
            "trailing": False,
        }
        last_entry_ts = entry_ts

    if position is not None:
        last = base[-1]
        exit_fill = float(last["close"]) * (1.0 - slip)
        quantity = float(position["quantity"])
        net_proceeds = quantity * exit_fill * (1.0 - fee)
        net_cost = float(position["notional"]) * (1.0 + fee)
        pnl = net_proceeds - net_cost
        trades.append({
            "symbol": pair,
            "entry_ts": int(position["entry_ts"]),
            "exit_ts": int(last["ts"]) + 60,
            "entry": float(position["entry"]),
            "exit": exit_fill,
            "reason": "END_OF_DATA",
            "net_pnl_usdt": pnl,
            "net_pnl_pct": (pnl / net_cost * 100.0) if net_cost else 0.0,
            "held_minutes": len(base) - 1 - int(position["entry_index"]),
        })

    training = [trade for trade in trades if trade["entry_ts"] < test_start]
    testing = [trade for trade in trades if trade["entry_ts"] >= test_start]
    return {
        "symbol": pair,
        "candles_1m": len(base),
        "candles_5m": len(medium),
        "candles_15m": len(long),
        "train": _metrics(training, 1000.0),
        "test": _metrics(testing, 1000.0),
        "trades": trades,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Gate.io real-candle 1m scalp backtest")
    parser.add_argument("--days", type=int, default=26)
    parser.add_argument("--pairs", default="BTC_USDT,ETH_USDT,SOL_USDT,XRP_USDT")
    parser.add_argument("--fee-pct", type=float, default=0.10, help="Taker fee on each side, percent")
    parser.add_argument("--slippage-bps", type=float, default=5.0, help="Adverse slippage on each side in basis points")
    parser.add_argument("--notional", type=float, default=25.0, help="Fixed quote notional per trade")
    parser.add_argument("--output-dir", default="backtest-results")
    parser.add_argument("--min-test-trades", type=int, default=30)
    parser.add_argument("--min-win-rate", type=float, default=60.0)
    parser.add_argument("--min-profit-factor", type=float, default=1.05)
    parser.add_argument("--max-drawdown", type=float, default=10.0)
    parser.add_argument("--require-pass", action="store_true")
    args = parser.parse_args()

    if args.days < 7:
        parser.error("--days must be at least 7")
    if args.fee_pct < 0 or args.slippage_bps < 0 or args.notional <= 0:
        parser.error("fees/slippage must be nonnegative and notional must be positive")

    pairs = [part.strip().upper() for part in args.pairs.split(",") if part.strip()]
    if not pairs:
        parser.error("At least one pair is required")

    now = int(time.time())
    period_start = now - args.days * 86400
    # Three extra days provide at least 200 completed 15m bars for warm-up.
    # Keep three days of warm-up while staying inside a recent 29-day window.
    fetch_start = period_start - 3 * 86400
    test_start = period_start + int(args.days * 86400 * 0.70)
    settings = deepcopy(DEFAULT_SETTINGS)
    settings["timeframe"] = "1m"
    settings["long_timeframe"] = "5m"
    settings["very_long_timeframe"] = "15m"
    settings["use_closed_candles_only"] = False  # every row passed to the simulator is closed
    settings["candle_limit"] = 500

    print(f"Gate.io historical data: {utc_text(period_start)} -> {utc_text(now)}", flush=True)
    print(f"Pairs: {', '.join(pairs)} | Fee: {args.fee_pct:.4f}% each side | Slippage: {args.slippage_bps:.2f} bps each side", flush=True)

    pair_results = []
    session = requests.Session()
    for pair in pairs:
        print(f"Downloading {pair} 1m/5m/15m candles...", flush=True)
        base = fetch_history(pair, "1m", fetch_start, now, session)
        medium = fetch_history(pair, "5m", fetch_start, now, session)
        long = fetch_history(pair, "15m", fetch_start, now, session)
        if not base:
            print(f"SKIP {pair}: no historical candles returned", flush=True)
            continue
        result = simulate_pair(
            pair, base, medium, long, period_start, test_start,
            args.notional, args.fee_pct, args.slippage_bps, settings,
        )
        pair_results.append(result)
        print(
            f"{pair}: test trades={result['test']['trades']}, "
            f"win rate={result['test']['win_rate_pct']:.1f}%, "
            f"PF={result['test']['profit_factor']:.2f}, "
            f"net={result['test']['net_pnl_usdt']:.2f} USDT",
            flush=True,
        )

    if not pair_results:
        raise RuntimeError("No symbols could be backtested; check Gate.io public API availability and pair names.")

    all_trades = [trade for result in pair_results for trade in result["trades"]]
    train_start = [t for t in all_trades if t["entry_ts"] < test_start]
    test_trades = [t for t in all_trades if t["entry_ts"] >= test_start]
    train_metrics = _metrics(train_start, 1000.0)
    test_metrics = _metrics(test_trades, 1000.0)
    passed = bool(
        test_metrics["trades"] >= args.min_test_trades
        and test_metrics["win_rate_pct"] >= args.min_win_rate
        and test_metrics["profit_factor"] >= args.min_profit_factor
        and test_metrics["net_pnl_usdt"] > 0
        and test_metrics["max_drawdown_pct"] <= args.max_drawdown
    )

    report = {
        "exchange": "Gate.io Spot",
        "data_endpoint": f"{BASE_URL}/spot/candlesticks",
        "strategy": "1m EMA9/21/200 + daily VWAP + quote-volume surge + Wilder RSI + 5m/15m trend confirmation",
        "period": {"start_utc": utc_text(period_start), "end_utc": utc_text(now), "days": args.days,
                   "out_of_sample_start_utc": utc_text(test_start), "split": "70% train / 30% out-of-sample"},
        "execution_model": {
            "entry": "next 1m candle open, adverse buy slippage",
            "exit": "OHLC stop/target; if both touched in one candle, stop first; adverse sell slippage",
            "fee_pct_each_side": args.fee_pct,
            "slippage_bps_each_side": args.slippage_bps,
            "fixed_notional_usdt_per_trade": args.notional,
            "portfolio_limitation": "Each pair is simulated independently; cross-symbol max-open-trade and shared-balance constraints are not modeled.",
        },
        "acceptance_criteria": {
            "minimum_out_of_sample_trades": args.min_test_trades,
            "minimum_win_rate_pct": args.min_win_rate,
            "minimum_profit_factor": args.min_profit_factor,
            "positive_net_pnl": True,
            "maximum_drawdown_pct": args.max_drawdown,
            "passed": passed,
        },
        "training": train_metrics,
        "out_of_sample": test_metrics,
        "per_pair": [
            {k: v for k, v in result.items() if k != "trades"} for result in pair_results
        ],
        "trades": sorted(all_trades, key=lambda trade: trade["entry_ts"]),
    }

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / "backtest_report.json"
    trades_path = output / "backtest_trades.csv"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    with trades_path.open("w", newline="", encoding="utf-8") as handle:
        import csv
        fields = ["symbol", "entry_ts", "exit_ts", "entry", "exit", "reason", "net_pnl_usdt", "net_pnl_pct", "held_minutes"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(all_trades)

    print("\nOUT-OF-SAMPLE RESULTS", flush=True)
    print(json.dumps(test_metrics, indent=2), flush=True)
    print(f"Acceptance: {'PASS' if passed else 'FAIL'}", flush=True)
    print(f"Report: {report_path} | Trades: {trades_path}", flush=True)
    if args.require_pass and not passed:
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"BACKTEST ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
