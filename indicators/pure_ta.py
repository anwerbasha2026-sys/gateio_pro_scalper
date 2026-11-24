import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Candle tuple/dict format:
# {"ts": int_sec, "open": float, "high": float, "low": float, "close": float, "volume": float}


def target_index(length: int, closed_only: bool = True) -> int:
    if closed_only and length >= 2:
        return length - 2
    return length - 1


def ema_series(values: List[float], span: int) -> List[float]:
    """Pure-Python EMA matching pandas ewm(span=span, adjust=False).mean()."""
    if not values:
        return []
    alpha = 2.0 / (span + 1.0)
    out = [values[0]]
    prev = values[0]
    for v in values[1:]:
        prev = alpha * v + (1.0 - alpha) * prev
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

    closes = [c["close"] for c in candles]
    e9 = ema_series(closes, 9)
    e21 = ema_series(closes, 21)
    e200 = ema_series(closes, 200)

    idx = target_index(len(candles), closed_only=closed_only)
    stop = max(0, idx - max(1, int(crossover_lookback)))

    score = 0
    cross_idx = idx
    for i in range(idx, stop, -1):
        bull_9_21 = (e9[i] > e21[i]) and (e9[i - 1] <= e21[i - 1])
        if not bull_9_21:
            continue
        bull_21_200 = (e21[i] > e200[i]) and (e21[i - 1] <= e200[i - 1])
        score = 100 if bull_21_200 else 90
        cross_idx = i
        break

    return {
        "idx": idx,
        "cross_idx": cross_idx,
        "score": score,
        "close": closes[idx],
        "ema9": e9[idx],
        "ema21": e21[idx],
        "ema200": e200[idx],
    }


def calculate_daily_vwap(
    candles: List[Dict[str, float]],
    closed_only: bool = True,
) -> Optional[float]:
    """Pure-Python Daily Anchored VWAP resetting at 00:00 UTC."""
    if not candles or len(candles) < 2:
        return None

    idx = target_index(len(candles), closed_only=closed_only)
    target_day = datetime.fromtimestamp(candles[idx]["ts"], tz=timezone.utc).date()

    cum_tp_vol = 0.0
    cum_vol = 0.0
    for c in candles[: idx + 1]:
        c_day = datetime.fromtimestamp(c["ts"], tz=timezone.utc).date()
        if c_day != target_day:
            continue
        tp = (c["high"] + c["low"] + c["close"]) / 3.0
        vol = c["volume"]
        cum_tp_vol += tp * vol
        cum_vol += vol

    if cum_vol <= 0:
        return None
    return cum_tp_vol / cum_vol


def analyze_volume(
    candles: List[Dict[str, float]],
    period: int = 20,
    multiplier: float = 2.0,
    min_z_score: float = 1.5,
    idx: Optional[int] = None,
    closed_only: bool = True,
) -> Optional[Dict[str, Any]]:
    if not candles or len(candles) < period + 2:
        return None

    t_idx = target_index(len(candles), closed_only=closed_only) if idx is None else idx
    if t_idx < period or t_idx >= len(candles):
        return None

    hist = [candles[i]["volume"] for i in range(t_idx - period, t_idx)]
    c_vol = candles[t_idx]["volume"]
    avg_vol = sum(hist) / len(hist)
    if avg_vol <= 0:
        return {
            "volume": c_vol,
            "avg_volume": 0.0,
            "vol_ratio": 0.0,
            "z_score": 0.0,
            "is_surge": False,
            "is_z_surge": False,
        }

    variance = sum((v - avg_vol) ** 2 for v in hist) / len(hist)
    std_vol = math.sqrt(variance)
    vol_ratio = c_vol / avg_vol
    z_score = (c_vol - avg_vol) / std_vol if std_vol > 0 else 0.0

    return {
        "volume": c_vol,
        "avg_volume": avg_vol,
        "vol_ratio": vol_ratio,
        "z_score": z_score,
        "is_surge": c_vol > (avg_vol * multiplier),
        "is_z_surge": z_score >= min_z_score,
    }


def calculate_rsi(closes: List[float], period: int = 14) -> Optional[List[float]]:
    """Pure-Python Wilder RSI(14)."""
    if not closes or len(closes) < period + 2:
        return None

    alpha = 1.0 / period
    rsi_out = [50.0]
    avg_gain = 0.0
    avg_loss = 0.0

    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gain = delta if delta > 0 else 0.0
        loss = -delta if delta < 0 else 0.0
        if i == 1:
            avg_gain = gain
            avg_loss = loss
        else:
            avg_gain = alpha * gain + (1.0 - alpha) * avg_gain
            avg_loss = alpha * loss + (1.0 - alpha) * avg_loss

        if avg_loss == 0.0:
            rsi_out.append(100.0 if avg_gain > 0 else 50.0)
        else:
            rs = avg_gain / avg_loss
            rsi_out.append(100.0 - (100.0 / (1.0 + rs)))

    return rsi_out
