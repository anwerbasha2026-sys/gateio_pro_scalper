import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Candle format: ts (seconds), open, high, low, close, volume (quote volume).


def target_index(length: int, closed_only: bool = True) -> int:
    if closed_only and length >= 2:
        return length - 2
    return length - 1


def ema_series(values: List[float], span: int) -> List[float]:
    """Pure-Python EMA with adjust=False semantics."""
    if span < 1:
        raise ValueError("EMA span must be positive")
    if not values:
        return []
    alpha = 2.0 / (span + 1.0)
    out = [float(values[0])]
    prev = float(values[0])
    for value in values[1:]:
        prev = alpha * float(value) + (1.0 - alpha) * prev
        out.append(prev)
    return out


def compute_ema_signals(
    candles: List[Dict[str, float]],
    use_ema200: bool = True,
    crossover_lookback: int = 3,
    closed_only: bool = True,
) -> Optional[Dict[str, Any]]:
    min_len = 202 if use_ema200 else 30
    if not candles or len(candles) < min_len:
        return None

    closes = [float(c["close"]) for c in candles]
    e9 = ema_series(closes, 9)
    e21 = ema_series(closes, 21)
    e200 = ema_series(closes, 200)
    idx = target_index(len(candles), closed_only=closed_only)
    lookback = max(1, int(crossover_lookback))
    stop = max(0, idx - lookback)

    cross_idx = None
    cross_200 = False
    for i in range(idx, stop, -1):
        crossed = e9[i] > e21[i] and e9[i - 1] <= e21[i - 1]
        if crossed:
            cross_idx = i
            cross_200 = e21[i] > e200[i] and e21[i - 1] <= e200[i - 1]
            break

    return {
        "idx": idx,
        "cross_idx": cross_idx,
        "cross_age": (idx - cross_idx) if cross_idx is not None else None,
        "score": 100 if cross_200 else (90 if cross_idx is not None else 0),
        "close": closes[idx],
        "ema9": e9[idx],
        "ema21": e21[idx],
        "ema200": e200[idx],
        "ema21_rising": idx >= 3 and e21[idx] > e21[idx - 3],
        "ema200_rising": idx >= 5 and e200[idx] >= e200[idx - 5],
    }


def calculate_daily_vwap(
    candles: List[Dict[str, float]],
    closed_only: bool = True,
) -> Optional[float]:
    """Daily UTC VWAP using quote-currency volume for the supplied session data."""
    if not candles:
        return None
    idx = target_index(len(candles), closed_only=closed_only)
    target_day = datetime.fromtimestamp(candles[idx]["ts"], tz=timezone.utc).date()

    cum_tp_vol = 0.0
    cum_vol = 0.0
    for candle in candles[: idx + 1]:
        candle_day = datetime.fromtimestamp(candle["ts"], tz=timezone.utc).date()
        if candle_day != target_day:
            continue
        typical_price = (
            float(candle["high"]) + float(candle["low"]) + float(candle["close"])
        ) / 3.0
        volume = float(candle.get("quote_volume", candle["volume"]))
        if volume <= 0:
            continue
        cum_tp_vol += typical_price * volume
        cum_vol += volume
    return cum_tp_vol / cum_vol if cum_vol > 0 else None


def analyze_volume(
    candles: List[Dict[str, float]],
    period: int = 20,
    multiplier: float = 2.0,
    min_z_score: float = 1.5,
    idx: Optional[int] = None,
    closed_only: bool = True,
) -> Optional[Dict[str, Any]]:
    if period < 2 or not candles or len(candles) < period + 1:
        return None
    t_idx = target_index(len(candles), closed_only=closed_only) if idx is None else idx
    if t_idx < period or t_idx >= len(candles):
        return None

    history = [float(candles[i]["volume"]) for i in range(t_idx - period, t_idx)]
    current_volume = float(candles[t_idx]["volume"])
    average = sum(history) / len(history)
    if average <= 0:
        return {
            "volume": current_volume, "avg_volume": 0.0, "vol_ratio": 0.0,
            "z_score": 0.0, "is_surge": False, "is_z_surge": False,
        }

    variance = sum((value - average) ** 2 for value in history) / len(history)
    std_dev = math.sqrt(variance)
    ratio = current_volume / average
    z_score = (current_volume - average) / std_dev if std_dev > 0 else 0.0
    return {
        "volume": current_volume,
        "avg_volume": average,
        "vol_ratio": ratio,
        "z_score": z_score,
        "is_surge": current_volume >= average * multiplier,
        "is_z_surge": z_score >= min_z_score,
    }


def calculate_rsi(closes: List[float], period: int = 14) -> Optional[List[float]]:
    """Wilder RSI using the standard initial n-period average gain/loss."""
    if period < 2 or not closes or len(closes) < period + 1:
        return None

    result = [50.0] * len(closes)
    gains = []
    losses = []
    for i in range(1, period + 1):
        delta = float(closes[i]) - float(closes[i - 1])
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    def to_rsi(gain: float, loss: float) -> float:
        if loss <= 0:
            return 100.0 if gain > 0 else 50.0
        rs = gain / loss
        return 100.0 - (100.0 / (1.0 + rs))

    result[period] = to_rsi(avg_gain, avg_loss)
    for i in range(period + 1, len(closes)):
        delta = float(closes[i]) - float(closes[i - 1])
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = ((period - 1) * avg_gain + gain) / period
        avg_loss = ((period - 1) * avg_loss + loss) / period
        result[i] = to_rsi(avg_gain, avg_loss)
    return result


def calculate_atr(
    candles: List[Dict[str, float]],
    period: int = 14,
) -> Optional[List[float]]:
    """Wilder ATR series; values before the initial period are zero."""
    if period < 1 or not candles or len(candles) < period + 1:
        return None

    true_ranges = []
    previous_close = float(candles[0]["close"])
    for candle in candles[1:]:
        high = float(candle["high"])
        low = float(candle["low"])
        close_prev = previous_close
        true_ranges.append(max(high - low, abs(high - close_prev), abs(low - close_prev)))
        previous_close = float(candle["close"])

    result = [0.0] * len(candles)
    atr = sum(true_ranges[:period]) / period
    result[period] = atr
    for i in range(period + 1, len(candles)):
        atr = ((period - 1) * atr + true_ranges[i - 1]) / period
        result[i] = atr
    return result
