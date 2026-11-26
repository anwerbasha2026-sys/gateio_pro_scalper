from typing import Any, Dict, List, Optional

from indicators.pure_ta import (
    analyze_volume,
    calculate_atr,
    calculate_daily_vwap,
    calculate_rsi,
    compute_ema_signals,
    ema_series,
    target_index,
)


def evaluate_ema_vwap_vol(
    candles: List[Dict[str, float]],
    settings: Dict[str, Any],
    vwap_candles: Optional[List[Dict[str, float]]] = None,
) -> Dict[str, Any]:
    """Conservative 1m long-only entry: fresh cross, trend, VWAP, volume and candle quality."""
    closed_only = bool(settings.get("use_closed_candles_only", True))
    use_ema200 = bool(settings.get("use_ema200", True))
    lookback = int(settings.get("crossover_lookback", 4))
    vol_period = int(settings.get("volume_period", 20))
    idx = target_index(len(candles), closed_only=closed_only) if candles else -1
    if idx < 202:
        return {"signal": False}

    ema_res = compute_ema_signals(
        candles,
        use_ema200=use_ema200,
        crossover_lookback=lookback,
        closed_only=closed_only,
    )
    if ema_res is None or ema_res["cross_idx"] is None:
        return {"signal": False}

    closes = [float(c["close"]) for c in candles]
    rsi_series = calculate_rsi(closes, 14)
    atr_series = calculate_atr(candles, 14)
    # Prefer a 5m session window (300 bars ~= 25h) for a true UTC-day VWAP;
    # fall back to the entry series for standalone/test use.
    vwap_source = vwap_candles if vwap_candles else candles
    vwap_value = calculate_daily_vwap(vwap_source, closed_only=closed_only)
    volume = analyze_volume(
        candles,
        period=vol_period,
        multiplier=float(settings.get("volume_multiplier", 1.5)),
        min_z_score=float(settings.get("min_z_score", 1.0)),
        idx=idx,
        closed_only=closed_only,
    )
    if rsi_series is None or atr_series is None or vwap_value is None or volume is None:
        return {"signal": False}

    candle = candles[idx]
    price = float(candle["close"])
    open_price = float(candle["open"])
    high = float(candle["high"])
    low = float(candle["low"])
    atr = float(atr_series[idx])
    rsi_value = float(rsi_series[idx])
    candle_range = max(high - low, 1e-12)
    close_location = (price - low) / candle_range
    atr_pct = (atr / price * 100.0) if price > 0 else 0.0
    ema_extension_atr = (price - ema_res["ema9"]) / atr if atr > 0 else 999.0

    rsi_min = float(settings.get("rsi_min", 52.0))
    rsi_max = float(settings.get("rsi_max", 68.0))
    min_close_location = float(settings.get("min_candle_close_location", 0.65))
    max_extension = float(settings.get("max_ema_extension_atr", 0.8))
    min_atr_pct = float(settings.get("min_atr_pct", 0.03))
    max_atr_pct = float(settings.get("max_atr_pct", 1.5))

    checks = {
        "fresh_bull_cross": ema_res["cross_age"] <= lookback,
        "ema_alignment": ema_res["ema9"] > ema_res["ema21"] and price > ema_res["ema9"],
        "ema_slope": ema_res["ema21_rising"],
        "ema200": (price > ema_res["ema200"] and ema_res["ema200_rising"]) if use_ema200 else True,
        "above_vwap": price > vwap_value,
        "volume_surge": volume["is_surge"],
        "volume_zscore": volume["is_z_surge"] if settings.get("use_z_score", True) else True,
        "rsi_band": rsi_min <= rsi_value <= rsi_max,
        "bullish_candle": price > open_price and close_location >= min_close_location,
        "not_overextended": 0.0 <= ema_extension_atr <= max_extension,
        "atr_filter": min_atr_pct <= atr_pct <= max_atr_pct,
    }
    score = int(round(100.0 * sum(1 for passed in checks.values() if passed) / len(checks)))
    required = all(checks.values())

    return {
        "signal": bool(required),
        "score": score,
        "checks": checks,
        "price": price,
        "atr": atr,
        "atr_pct": atr_pct,
        "ema_score": ema_res["score"],
        "ema_cross_age": ema_res["cross_age"],
        "vwap": vwap_value,
        "vol_ratio": volume["vol_ratio"],
        "z_score": volume["z_score"],
        "rsi": rsi_value,
        "close_location": close_location,
    }


def evaluate_momentum(
    candles: List[Dict[str, float]],
    settings: Dict[str, Any],
    vwap_candles: Optional[List[Dict[str, float]]] = None,
) -> Dict[str, Any]:
    if not candles or len(candles) < 202:
        return {"signal": False}
    closed_only = bool(settings.get("use_closed_candles_only", True))
    idx = target_index(len(candles), closed_only=closed_only)
    closes = [float(c["close"]) for c in candles]
    e9 = ema_series(closes, 9)
    e21 = ema_series(closes, 21)
    rsi = calculate_rsi(closes, 14)
    atr = calculate_atr(candles, 14)
    vwap_source = vwap_candles if vwap_candles else candles
    vwap_value = calculate_daily_vwap(vwap_source, closed_only=closed_only)
    volume = analyze_volume(
        candles,
        period=int(settings.get("volume_period", 20)),
        multiplier=float(settings.get("volume_multiplier", 1.5)),
        min_z_score=float(settings.get("min_z_score", 1.0)),
        idx=idx,
        closed_only=closed_only,
    )
    if rsi is None or atr is None or vwap_value is None or volume is None:
        return {"signal": False}

    price = closes[idx]
    rsi_value = rsi[idx]
    range_value = max(float(candles[idx]["high"]) - float(candles[idx]["low"]), 1e-12)
    close_location = (price - float(candles[idx]["low"])) / range_value
    passed = bool(
        e9[idx] > e21[idx]
        and e21[idx] > e21[max(0, idx - 3)]
        and price > e9[idx]
        and price > vwap_value
        and volume["is_surge"]
        and volume["is_z_surge"]
        and float(settings.get("rsi_min", 52.0)) <= rsi_value <= float(settings.get("rsi_max", 68.0))
        and price > float(candles[idx]["open"])
        and close_location >= float(settings.get("min_candle_close_location", 0.65))
        and atr[idx] > 0
    )
    return {
        "signal": passed,
        "price": price,
        "atr": atr[idx],
        "vwap": vwap_value,
        "vol_ratio": volume["vol_ratio"],
        "z_score": volume["z_score"],
        "rsi": rsi_value,
    }


def check_htf_15m_trend(candles: Optional[List[Dict[str, float]]], closed_only: bool = True) -> bool:
    if not candles or len(candles) < 202:
        return False
    closes = [float(c["close"]) for c in candles]
    e9 = ema_series(closes, 9)
    e21 = ema_series(closes, 21)
    e200 = ema_series(closes, 200)
    idx = target_index(len(candles), closed_only=closed_only)
    return bool(e9[idx] > e21[idx] > e200[idx] and e21[idx] > e21[idx - 3])


def check_htf_1h_trend(candles: Optional[List[Dict[str, float]]], closed_only: bool = True) -> bool:
    if not candles or len(candles) < 202:
        return False
    closes = [float(c["close"]) for c in candles]
    e9 = ema_series(closes, 9)
    e21 = ema_series(closes, 21)
    e200 = ema_series(closes, 200)
    idx = target_index(len(candles), closed_only=closed_only)
    return bool(closes[idx] > e200[idx] and e9[idx] > e21[idx] and e200[idx] >= e200[idx - 5])


def scan_symbol(api: Any, pair: str, settings: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Scan one Gate.io spot pair and require completed higher-timeframe alignment."""
    candles = api.fetch_candles(pair, interval=settings["timeframe"], limit=int(settings["candle_limit"]))
    if not candles:
        return None

    enable_evv = bool(settings.get("enable_ema_vwap_vol", True))
    enable_momentum = bool(settings.get("enable_momentum", False))
    if not (enable_evv or enable_momentum):
        return None

    closed_only = bool(settings.get("use_closed_candles_only", True))
    # Fetch enough 5m bars to include the full current UTC session for VWAP.
    medium = None
    if settings.get("use_htf_filter", True) and settings.get("long_timeframe") == "5m":
        medium = api.fetch_candles(pair, interval="5m", limit=300)
    vwap_candles = medium or api.fetch_candles(pair, interval="5m", limit=300)
    if settings.get("use_htf_filter", True) and medium is None:
        medium = api.fetch_candles(pair, interval=settings["long_timeframe"], limit=300)

    evv = evaluate_ema_vwap_vol(candles, settings, vwap_candles=vwap_candles) if enable_evv else {"signal": False}
    momentum = evaluate_momentum(candles, settings, vwap_candles=vwap_candles) if enable_momentum else {"signal": False}
    strategies = []

    if enable_evv and enable_momentum:
        if not (evv.get("signal") and momentum.get("signal")):
            return None
        strategies = ["EMA_VWAP_VOL", "MOMENTUM"]
    elif enable_evv:
        if not evv.get("signal"):
            return None
        strategies = ["EMA_VWAP_VOL"]
    else:
        if not momentum.get("signal"):
            return None
        strategies = ["MOMENTUM"]

    htf_medium_ok = True
    htf_long_ok = True
    if settings.get("use_htf_filter", True):
        htf_medium_ok = check_htf_15m_trend(medium, closed_only=closed_only)
        if not htf_medium_ok:
            return None
    if settings.get("use_very_long_filter", True):
        long = api.fetch_candles(pair, interval=settings["very_long_timeframe"], limit=220)
        htf_long_ok = check_htf_1h_trend(long, closed_only=closed_only)
        if not htf_long_ok:
            return None

    primary = evv if evv.get("signal") else momentum
    return {
        "symbol": pair,
        "timeframe": settings["timeframe"],
        "price": primary["price"],
        "atr": primary.get("atr"),
        "strategies": strategies,
        "score": primary.get("score"),
        "ema_score": evv.get("ema_score"),
        "vwap": primary.get("vwap"),
        "vol_ratio": primary.get("vol_ratio"),
        "z_score": primary.get("z_score"),
        "rsi": primary.get("rsi"),
        "htf_15m_ok": htf_medium_ok,
        "htf_1h_ok": htf_long_ok,
    }
