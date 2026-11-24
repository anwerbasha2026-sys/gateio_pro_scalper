import csv
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import requests


class SignalNotifier:
    def __init__(self, settings: Dict[str, Any], data_dir: Optional[str] = None):
        self._data_dir = Path(data_dir) if data_dir else Path(__file__).resolve().parent.parent
        self._last_alert_ts: Dict[str, float] = {}
        self.update_settings(settings)

    def update_settings(self, settings: Dict[str, Any]) -> None:
        self.cooldown_sec = max(0, int(settings.get("signal_cooldown_min", 15)) * 60)
        tg = settings.get("telegram", {})
        self.tg_enabled = bool(tg.get("enabled", False))
        self.tg_token = str(tg.get("bot_token", "")).strip()
        self.tg_chat_id = str(tg.get("chat_id", "")).strip()
        self.log_csv = bool(settings.get("log_csv", True))
        self.csv_path = self._data_dir / "signals_log.csv"
        if self.log_csv:
            self._ensure_header()

    def _ensure_header(self) -> None:
        if not self.csv_path.exists():
            self.csv_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.csv_path, "w", newline="", encoding="utf-8-sig") as f:
                csv.writer(f).writerow(
                    ["timestamp_utc", "symbol", "timeframe", "price", "strategies", "ema_score", "vol_ratio", "z_score"]
                )

    def can_alert(self, symbol: str, now_ts: Optional[float] = None) -> bool:
        now = time.time() if now_ts is None else now_ts
        last = self._last_alert_ts.get(symbol)
        if last is not None and (now - last) < self.cooldown_sec:
            return False
        self._last_alert_ts[symbol] = now
        return True

    def send_telegram(self, text: str) -> bool:
        if not (self.tg_enabled and self.tg_token and self.tg_chat_id):
            return False
        url = f"https://api.telegram.org/bot{self.tg_token}/sendMessage"
        try:
            r = requests.post(
                url,
                json={"chat_id": self.tg_chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=10,
            )
            return r.status_code == 200
        except Exception:
            return False

    def dispatch(self, payload: Dict[str, Any]) -> bool:
        if not self.can_alert(payload["symbol"]):
            return False
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        if self.log_csv:
            with open(self.csv_path, "a", newline="", encoding="utf-8-sig") as f:
                csv.writer(f).writerow(
                    [
                        ts,
                        payload["symbol"],
                        payload["timeframe"],
                        f"{payload['price']:.8g}",
                        "+".join(payload["strategies"]),
                        payload.get("ema_score", ""),
                        f"{payload['vol_ratio']:.2f}" if payload.get("vol_ratio") is not None else "",
                        f"{payload['z_score']:.2f}" if payload.get("z_score") is not None else "",
                    ]
                )
        if self.tg_enabled:
            msg = (
                f"🚀 *Gate.io Spot Signal: {payload['symbol']}*\n"
                f"• TF: `{payload['timeframe']}` | Price: `{payload['price']:.8g}`\n"
                f"• Strategy: `{' + '.join(payload['strategies'])}`\n"
                f"• Vol: `{payload.get('vol_ratio', 0):.2f}x` | UTC: `{ts}`"
            )
            self.send_telegram(msg)
        return True
