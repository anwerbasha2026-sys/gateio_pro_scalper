import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


@dataclass
class Position:
    symbol: str
    mode: str
    entry_price: float
    qty: float
    invested_usdt: float
    stop_loss: float
    take_profit: float
    highest_price: float
    trailing_active: bool = False
    current_price: float = 0.0
    opened_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%H:%M:%S")
    )
    strategy: str = ""
    order_id: str = ""

    @property
    def unrealized_pnl_usdt(self) -> float:
        p = self.current_price if self.current_price > 0 else self.entry_price
        return (p - self.entry_price) * self.qty

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        p = self.current_price if self.current_price > 0 else self.entry_price
        return ((p - self.entry_price) / self.entry_price) * 100.0


class TradeManager:
    def __init__(
        self,
        settings: Dict[str, Any],
        data_dir: Optional[str] = None,
        log_cb: Optional[Callable[[str], None]] = None,
    ):
        self.settings = settings
        self.log_cb = log_cb
        self._data_dir = Path(data_dir) if data_dir else Path(__file__).resolve().parent.parent
        trading = settings.get("trading", {})
        self.paper_balance: float = float(trading.get("paper_balance_usdt", 1000.0))
        self.live_balance: Optional[float] = None
        self.positions: Dict[str, Position] = {}
        self.closed_trades: List[Dict[str, Any]] = []
        self.realized_pnl: float = 0.0
        self.wins: int = 0
        self.losses: int = 0
        self.trades_csv = self._data_dir / "trades_history.csv"
        self._ensure_csv()

    def _log(self, msg: str) -> None:
        if self.log_cb:
            self.log_cb(msg)

    def _ensure_csv(self) -> None:
        if not self.trades_csv.exists():
            self.trades_csv.parent.mkdir(parents=True, exist_ok=True)
            with open(self.trades_csv, "w", newline="", encoding="utf-8-sig") as f:
                csv.writer(f).writerow(
                    ["closed_at", "mode", "symbol", "entry", "exit", "pnl_usdt", "pnl_pct", "reason"]
                )

    def update_settings(self, settings: Dict[str, Any]) -> None:
        self.settings = settings

    def reset_paper(self, balance: float) -> None:
        self.paper_balance = float(balance)
        for sym in [s for s, p in self.positions.items() if p.mode == "paper"]:
            del self.positions[sym]
        self.realized_pnl = 0.0
        self.wins = 0
        self.losses = 0
        self._log(f"Paper balance reset to {self.paper_balance:.2f} USDT")

    @property
    def cfg(self) -> Dict[str, Any]:
        return self.settings.get("trading", {})

    @property
    def mode(self) -> str:
        return str(self.cfg.get("mode", "paper")).lower()

    @property
    def win_rate(self) -> float:
        tot = self.wins + self.losses
        return (self.wins / tot * 100.0) if tot > 0 else 0.0

    def can_open(self, symbol: str) -> tuple[bool, str]:
        if not bool(self.cfg.get("auto_trade", True)):
            return False, "Auto-trade OFF"
        if symbol in self.positions:
            return False, "Already open"
        max_t = int(self.cfg.get("max_open_trades", 3))
        if len(self.positions) >= max_t:
            return False, f"Max open trades reached ({max_t})"
        amt = float(self.cfg.get("trade_amount_usdt", 25.0))
        if amt <= 0:
            return False, "Invalid trade amount"
        if self.mode == "paper" and self.paper_balance < amt:
            return False, f"Insufficient paper balance ({self.paper_balance:.2f})"
        return True, "OK"

    def open_trade(self, symbol: str, price: float, strategy: str, api: Any = None) -> Optional[Position]:
        ok, reason = self.can_open(symbol)
        if not ok:
            self._log(f"[SKIP {symbol}] {reason}")
            return None

        amt = float(self.cfg.get("trade_amount_usdt", 25.0))
        tp_pct = float(self.cfg.get("take_profit_pct", 1.5))
        sl_pct = float(self.cfg.get("stop_loss_pct", 1.0))
        fee_pct = float(self.cfg.get("fee_pct", 0.1))
        mode = self.mode

        if mode == "live":
            if api is None:
                self._log(f"[ERR] API client missing for live buy {symbol}")
                return None
            try:
                order = api.place_market_buy(symbol, amt)
                entry = order["avg_price"]
                qty = order["filled_qty"]
                invested = order["cost"]
                oid = order["order_id"]
                try:
                    self.live_balance = api.fetch_usdt_balance()
                except Exception:
                    pass
            except Exception as e:
                self._log(f"[LIVE BUY ERR {symbol}] {e}")
                return None
        else:
            entry = float(price)
            if entry <= 0:
                return None
            net_usdt = max(0.0, amt * (1.0 - fee_pct / 100.0))
            qty = net_usdt / entry
            invested = amt
            self.paper_balance -= amt
            oid = f"PAPER-{int(datetime.now().timestamp())}"

        sl = entry * (1.0 - sl_pct / 100.0)
        tp = entry * (1.0 + tp_pct / 100.0)
        pos = Position(
            symbol=symbol,
            mode=mode,
            entry_price=entry,
            qty=qty,
            invested_usdt=invested,
            stop_loss=sl,
            take_profit=tp,
            highest_price=entry,
            current_price=entry,
            strategy=strategy,
            order_id=oid,
        )
        self.positions[symbol] = pos
        self._log(f"[BUY {mode.upper()}] {symbol} @ {entry:.8g} | {invested:.2f} USDT | TP {tp:.8g} | SL {sl:.8g}")
        return pos

    def evaluate_price(self, symbol: str, live_price: float, api: Any = None) -> Optional[Dict[str, Any]]:
        pos = self.positions.get(symbol)
        if pos is None or live_price <= 0:
            return None

        pos.current_price = live_price
        if live_price > pos.highest_price:
            pos.highest_price = live_price

        if bool(self.cfg.get("trailing_stop_enabled", True)):
            act = float(self.cfg.get("trailing_activation_pct", 0.8))
            dist = float(self.cfg.get("trailing_distance_pct", 0.4))
            gain = ((pos.highest_price - pos.entry_price) / pos.entry_price) * 100.0
            if gain >= act:
                pos.trailing_active = True
                new_sl = pos.highest_price * (1.0 - dist / 100.0)
                if new_sl > pos.stop_loss:
                    pos.stop_loss = new_sl

        if live_price >= pos.take_profit:
            return self.close_trade(symbol, live_price, "TP", api=api)
        if live_price <= pos.stop_loss:
            return self.close_trade(symbol, live_price, "TRAIL_SL" if pos.trailing_active else "SL", api=api)
        return None

    def close_trade(self, symbol: str, exit_price: float, reason: str = "MANUAL", api: Any = None) -> Optional[Dict[str, Any]]:
        pos = self.positions.get(symbol)
        if pos is None:
            return None

        fee_pct = float(self.cfg.get("fee_pct", 0.1))
        if pos.mode == "live":
            if api is None:
                self._log(f"[ERR] API client missing for live sell {symbol}")
                return None
            try:
                order = api.place_market_sell(symbol, pos.qty)
                actual_exit = order["avg_price"]
                proceeds = order["cost"]
                try:
                    self.live_balance = api.fetch_usdt_balance()
                except Exception:
                    pass
            except Exception as e:
                self._log(f"[LIVE SELL ERR {symbol}] {e}")
                return None
        else:
            actual_exit = float(exit_price)
            proceeds = max(0.0, (pos.qty * actual_exit) * (1.0 - fee_pct / 100.0))
            self.paper_balance += proceeds

        pnl = proceeds - pos.invested_usdt
        pnl_pct = (pnl / pos.invested_usdt * 100.0) if pos.invested_usdt > 0 else 0.0
        self.realized_pnl += pnl
        if pnl >= 0:
            self.wins += 1
        else:
            self.losses += 1
        del self.positions[symbol]

        rec = {
            "closed_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "mode": pos.mode,
            "symbol": symbol,
            "entry": pos.entry_price,
            "exit": actual_exit,
            "pnl_usdt": pnl,
            "pnl_pct": pnl_pct,
            "reason": reason,
        }
        self.closed_trades.insert(0, rec)
        with open(self.trades_csv, "a", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(
                [rec["closed_at"], rec["mode"].upper(), symbol, f"{pos.entry_price:.8g}", f"{actual_exit:.8g}", f"{pnl:.4f}", f"{pnl_pct:.2f}%", reason]
            )

        sign = "+" if pnl >= 0 else ""
        self._log(f"[CLOSE {reason}] {symbol} @ {actual_exit:.8g} | PnL: {sign}{pnl:.2f} USDT ({sign}{pnl_pct:.2f}%)")
        return rec
