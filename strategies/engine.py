from typing import Any, Dict, List, Optional

from indicators.pure_ta import (
    analyze_volume,
    calculate_daily_vwap,
    calculate_rsi,
    compute_ema_signals,
    ema_series,
    target_index,
)


def evaluate_ema_vwap_vol(
    candles: List[Dict[str, float]],
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    closed_only = bool(settings.get("use_closed_candles_only", True))
    use_ema200 = bool(settings.get("use_ema200", True))
    lookback = int(settings.get("crossover_lookback", 3))
    vol_period = int(settings.get("volume_period", 20))
    vol_mult = float(settings.get("volume_multiplier", 2.0))
    use_z = bool(settings.get("use_z_score", False))
    min_z = float(settings.get("min_z_score", 1.5))

    ema_res = compute_ema_signals(
        candles,
        use_ema200=use_ema200,
        crossover_lookback=lookback,
        closed_only=closed_only,
    )
    if ema_res is None or ema_res["score"] < 90:
        return {"signal": False}

    vwap_val = calculate_daily_vwap(candles, closed_only=closed_only)
    if vwap_val is None:
        return {"signal": False}

    vol_res = analyze_volume(
        candles,
        period=vol_period,
        multiplier=vol_mult,
        min_z_score=min_z,
        idx=ema_res["cross_idx"],
        closed_only=closed_only,
    )
    if vol_res is None:
        return {"signal": False}

    price = ema_res["close"]
    ema_ok = ema_res["ema9"] > ema_res["ema21"]
    ema200_ok = (price > ema_res["ema200"]) if use_ema200 else True
    vwap_ok = price > vwap_val
    vol_ok = vol_res["is_surge"]
    z_ok = vol_res["is_z_surge"] if use_z else True

    return {
        "signal": bool(ema_ok and ema200_ok and vwap_ok and vol_ok and z_ok),
        "price": price,
        "ema_score": ema_res["score"],
        "vwap": vwap_val,
        "vol_ratio": vol_res["vol_ratio"],
        "z_score": vol_res["z_score"],
    }


def evaluate_momentum(
    candles: List[Dict[str, float]],
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    if not candles or len(candles) < 35:
        return {"signal": False}

    closed_only = bool(settings.get("use_closed_candles_only", True))
    idx = target_index(len(candles), closed_only=closed_only)
    if idx < 22:
        return {"signal": False}

    closes = [c["close"] for c in candles]
    e9 = ema_series(closes, 9)
    e21 = ema_series(closes, 21)
    rsi = calculate_rsi(closes, 14)
    vwap_val = calculate_daily_vwap(candles, closed_only=closed_only)
    vol_res = analyze_volume(
        candles,
        period=int(settings.get("volume_period", 20)),
        multiplier=1.5,
        idx=idx,
        closed_only=closed_only,
    )
    if rsi is None or vwap_val is None or vol_res is None:
        return {"signal": False}

    price = closes[idx]
    rsi_val = rsi[idx]
    ema_cross = (e9[idx - 1] <= e21[idx - 1]) and (e9[idx] > e21[idx])
    passed = bool(ema_cross and vol_res["is_surge"] and (50.0 < rsi_val < 70.0) and (price > vwap_val))

    return {
        "signal": passed,
        "price": price,
        "vwap": vwap_val,
        "vol_ratio": vol_res["vol_ratio"],
        "z_score": vol_res["z_score"],
        "rsi": rsi_val,
    }


def check_htf_15m_trend(candles: Optional[List[Dict[str, float]]], closed_only: bool = True) -> bool:
    if not candles or len(candles) < 202:
        return False
    closes = [c["close"] for c in candles]
    e9 = ema_series(closes, 9)
    e21 = ema_series(closes, 21)
    e200 = ema_series(closes, 200)
    idx = target_index(len(candles), closed_only=closed_only)
    return bool(e9[idx] > e21[idx] > e200[idx])


def check_htf_1h_trend(candles: Optional[List[Dict[str, float]]], closed_only: bool = True) -> bool:
    if not candles or len(candles) < 202:
        return False
    closes = [c["close"] for c in candles]
    e200 = ema_series(closes, 200)
    idx = target_index(len(candles), closed_only=closed_only)
    return bool(closes[idx] > e200[idx])


def scan_symbol(api: Any, pair: str, settings: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Lazy-fetch scanner for a single Gate.io pair (e.g. BTC_USDT)."""
    candles = api.fetch_candles(pair, interval=settings["timeframe"], limit=int(settings["candle_limit"]))
    if not candles:
        return None

    enable_evv = bool(settings.get("enable_ema_vwap_vol", True))
    enable_mom = bool(settings.get("enable_momentum", False))
    if not (enable_evv or enable_mom):
        return None

    evv_res = evaluate_ema_vwap_vol(candles, settings) if enable_evv else {"signal": False}
    mom_res = evaluate_momentum(candles, settings) if enable_mom else {"signal": False}

    strategies = []
    if enable_evv and enable_mom:
        if not (evv_res.get("signal") and mom_res.get("signal")):
            return None
        strategies = ["EMA_VWAP_VOL", "MOMENTUM"]
    elif enable_evv:
        if not evv_res.get("signal"):
            return None
        strategies = ["EMA_VWAP_VOL"]
    elif enable_mom:
        if not mom_res.get("signal"):
            return None
        strategies = ["MOMENTUM"]

    closed_only = bool(settings.get("use_closed_candles_only", True))
    htf_15m_ok = True
    htf_1h_ok = True

    if settings.get("use_htf_filter", True):
        c15 = api.fetch_candles(pair, interval=settings["long_timeframe"], limit=220)
        htf_15m_ok = check_htf_15m_trend(c15, closed_only=closed_only)
        if not htf_15m_ok:
            return None

    if settings.get("use_very_long_filter", True):
        c1h = api.fetch_candles(pair, interval=settings["very_long_timeframe"], limit=220)
        htf_1h_ok = check_htf_1h_trend(c1h, closed_only=closed_only)
        if not htf_1h_ok:
            return None

    primary = evv_res if evv_res.get("signal") else mom_res
    return {
        "symbol": pair,
        "timeframe": settings["timeframe"],
        "price": primary["price"],
        "strategies": strategies,
        "ema_score": evv_res.get("ema_score"),
        "vwap": primary.get("vwap"),
        "vol_ratio": primary.get("vol_ratio"),
        "z_score": primary.get("z_score"),
        "rsi": mom_res.get("rsi"),
        "htf_15m_ok": htf_15m_ok,
        "htf_1h_ok": htf_1h_ok,
    }
