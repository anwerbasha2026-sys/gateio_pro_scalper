import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_SETTINGS: Dict[str, Any] = {
    "strategy_profile_version": 2,
    "num_coins": 150,
    "min_quote_volume_usdt": 500000,
    "timeframe": "1m",
    "long_timeframe": "5m",
    "very_long_timeframe": "15m",
    "use_htf_filter": True,
    "use_very_long_filter": True,
    "use_ema200": True,
    "candle_limit": 600,
    "crossover_lookback": 4,
    "pullback_lookback_bars": 3,
    "pullback_ema_tolerance_atr": 0.20,
    "volume_period": 20,
    "volume_multiplier": 1.3,
    "use_z_score": True,
    "min_z_score": 0.8,
    "rsi_min": 52.0,
    "rsi_max": 68.0,
    "min_candle_close_location": 0.65,
    "max_ema_extension_atr": 1.0,
    "min_atr_pct": 0.015,
    "max_atr_pct": 0.8,
    "enable_ema_vwap_vol": True,
    "enable_momentum": False,
    "use_closed_candles_only": True,
    "max_workers": 6,
    "scan_interval_sec": 12,
    "signal_cooldown_min": 5,
    "excluded_stables": [
        "USDT", "USDC", "USD1", "BUSD", "DAI", "TUSD",
        "FDUSD", "USDE", "USDP", "EUR", "AEUR", "PYUSD",
    ],
    "trading": {
        "mode": "paper",
        "auto_trade": True,
        "live_trading_approved": False,
        "trade_amount_usdt": 25.0,
        "max_open_trades": 3,
        "paper_balance_usdt": 1000.0,
        "take_profit_pct": 0.75,
        "stop_loss_pct": 0.55,
        "use_atr_exits": True,
        "atr_target_multiplier": 1.5,
        "atr_stop_multiplier": 1.2,
        "min_take_profit_pct": 0.65,
        "min_stop_loss_pct": 0.45,
        "max_holding_minutes": 15,
        "break_even_activation_pct": 0.4,
        "trailing_stop_enabled": True,
        "trailing_activation_pct": 0.55,
        "trailing_distance_pct": 0.25,
        "fee_pct": 0.1,
        "slippage_pct": 0.05,
        "monitor_interval_sec": 3,
    },
    "gateio_api": {
        "api_key": "",
        "api_secret": "",
    },
    "telegram": {
        "enabled": False,
        "bot_token": "",
        "chat_id": "",
    },
    "log_csv": True,
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


def get_config_path(data_dir: Optional[str] = None) -> Path:
    if data_dir:
        base = Path(data_dir)
    else:
        base = Path(__file__).resolve().parent
    base.mkdir(parents=True, exist_ok=True)
    return base / "config.json"


def save_settings(settings_dict: Dict[str, Any], data_dir: Optional[str] = None) -> None:
    path = get_config_path(data_dir)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(settings_dict, f, indent=2, ensure_ascii=False)


def _migrate_strategy_profile(user_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Move untouched legacy defaults to the faster win-rate scalp profile."""
    if int(user_cfg.get("strategy_profile_version", 1) or 1) >= 2:
        return user_cfg

    legacy_top = {
        "timeframe": "5m",
        "long_timeframe": "15m",
        "very_long_timeframe": "1h",
        "candle_limit": 500,
        "rsi_min": 50.0,
        "rsi_max": 70.0,
        "min_candle_close_location": 0.60,
        "max_ema_extension_atr": 1.2,
        "min_atr_pct": 0.03,
        "max_atr_pct": 1.5,
        "scan_interval_sec": 15,
        "signal_cooldown_min": 10,
    }
    for key, legacy_value in legacy_top.items():
        if key not in user_cfg or user_cfg.get(key) == legacy_value:
            user_cfg[key] = deepcopy(DEFAULT_SETTINGS[key])

    trading = user_cfg.setdefault("trading", {})
    legacy_trading = {
        "take_profit_pct": 0.65,
        "atr_target_multiplier": 1.25,
        "atr_stop_multiplier": 1.6,
        "break_even_activation_pct": 0.5,
        "trailing_activation_pct": 0.75,
        "trailing_distance_pct": 0.3,
    }
    for key, legacy_value in legacy_trading.items():
        if key not in trading or trading.get(key) == legacy_value:
            trading[key] = deepcopy(DEFAULT_SETTINGS["trading"][key])

    user_cfg["strategy_profile_version"] = 2
    return user_cfg


def load_settings(data_dir: Optional[str] = None) -> Dict[str, Any]:
    path = get_config_path(data_dir)
    if not os.path.exists(path):
        save_settings(DEFAULT_SETTINGS, data_dir)
        return deepcopy(DEFAULT_SETTINGS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        if not isinstance(user_cfg, dict):
            raise ValueError("Settings file must contain a JSON object")
        user_cfg = _migrate_strategy_profile(user_cfg)
        merged = _deep_merge(DEFAULT_SETTINGS, user_cfg)
        save_settings(merged, data_dir)
        return merged
    except Exception:
        return deepcopy(DEFAULT_SETTINGS)
