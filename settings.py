import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_SETTINGS: Dict[str, Any] = {
    "num_coins": 150,
    "min_quote_volume_usdt": 500000,
    "timeframe": "5m",
    "long_timeframe": "15m",
    "very_long_timeframe": "1h",
    "use_htf_filter": True,
    "use_very_long_filter": True,
    "use_ema200": True,
    "candle_limit": 500,
    "crossover_lookback": 4,
    "volume_period": 20,
    "volume_multiplier": 1.5,
    "use_z_score": True,
    "min_z_score": 1.0,
    "rsi_min": 52.0,
    "rsi_max": 68.0,
    "min_candle_close_location": 0.65,
    "max_ema_extension_atr": 0.8,
    "min_atr_pct": 0.03,
    "max_atr_pct": 1.5,
    "enable_ema_vwap_vol": True,
    "enable_momentum": False,
    "use_closed_candles_only": True,
    "max_workers": 6,
    "scan_interval_sec": 15,
    "signal_cooldown_min": 10,
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
        "take_profit_pct": 0.65,
        "stop_loss_pct": 0.55,
        "use_atr_exits": True,
        "atr_target_multiplier": 1.25,
        "atr_stop_multiplier": 1.6,
        "min_take_profit_pct": 0.65,
        "min_stop_loss_pct": 0.45,
        "max_holding_minutes": 15,
        "break_even_activation_pct": 0.5,
        "trailing_stop_enabled": True,
        "trailing_activation_pct": 0.75,
        "trailing_distance_pct": 0.3,
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


def load_settings(data_dir: Optional[str] = None) -> Dict[str, Any]:
    path = get_config_path(data_dir)
    if not os.path.exists(path):
        save_settings(DEFAULT_SETTINGS, data_dir)
        return deepcopy(DEFAULT_SETTINGS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        return _deep_merge(DEFAULT_SETTINGS, user_cfg)
    except Exception:
        return deepcopy(DEFAULT_SETTINGS)
