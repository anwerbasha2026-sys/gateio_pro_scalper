import csv
import time
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
    opened_at: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    opened_ts: float = field(default_factory=time.time)
    strategy: str = ""
    order_id: str = ""
    atr_at_entry: float = 0.0

    @property
    def unrealized_pnl_usdt(self) -> float:
        price = self.current_price if self.current_price > 0 else self.entry_price
        return (price - self.entry_price) * self.qty

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        price = self.current_price if self.current_price > 0 else self.entry_price
        return ((price - self.entry_price) / self.entry_price) * 100.0


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
        self.paper_balance = float(trading.get("paper_balance_usdt", 1000.0))
        self.live_balance: Optional[float] = None
        self.positions: Dict[str, Position] = {}
        self.closed_trades: List[Dict[str, Any]] = []
        self.realized_pnl = 0.0
        self.wins = 0
        self.losses = 0
        self.trades_csv = self._data_dir / "trades_history.csv"
        self._ensure_csv()

    def _log(self, msg: str) -> None:
        if self.log_cb:
            self.log_cb(msg)

    def _ensure_csv(self) -> None:
        if not self.trades_csv.exists():
            self.trades_csv.parent.mkdir(parents=True, exist_ok=True)
            with open(self.trades_csv, "w", newline="", encoding="utf-8-sig") as handle:
                csv.writer(handle).writerow(
                    ["closed_at", "mode", "symbol", "entry", "exit", "pnl_usdt", "pnl_pct", "reason"]
                )

    def update_settings(self, settings: Dict[str, Any]) -> None:
        self.settings = settings

    def reset_paper(self, balance: float) -> None:
        self.paper_balance = float(balance)
        for symbol in [s for s, p in self.positions.items() if p.mode == "paper"]:
            del self.positions[symbol]
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
        total = self.wins + self.losses
        return (self.wins / total * 100.0) if total else 0.0

    def can_open(self, symbol: str) -> tuple[bool, str]:
        if not bool(self.cfg.get("auto_trade", True)):
            return False, "Auto-trade OFF"
        if self.mode == "live" and not bool(self.cfg.get("live_trading_approved", False)):
            return False, "LIVE is locked until historical backtest and Paper validation pass"
        if symbol in self.positions:
            return False, "Already open"
        max_trades = int(self.cfg.get("max_open_trades", 3))
        if len(self.positions) >= max_trades:
            return False, f"Max open trades reached ({max_trades})"
        amount = float(self.cfg.get("trade_amount_usdt", 25.0))
        if amount <= 0:
            return False, "Invalid trade amount"
        if self.mode == "paper" and self.paper_balance < amount:
            return False, f"Insufficient paper balance ({self.paper_balance:.2f})"
        return True, "OK"

    def _exit_distances(self, entry: float, atr: Optional[float]) -> tuple[float, float]:
        target_pct = float(self.cfg.get("take_profit_pct", 0.65))
        stop_pct = float(self.cfg.get("stop_loss_pct", 0.55))
        if bool(self.cfg.get("use_atr_exits", True)) and atr and atr > 0 and entry > 0:
            atr_pct = atr / entry * 100.0
            # UI TP/SL values remain effective floors; ATR expands levels when volatility requires it.
            stop_pct = max(
                stop_pct,
                atr_pct * float(self.cfg.get("atr_stop_multiplier", 1.6)),
            )
            target_pct = max(
                target_pct,
                atr_pct * float(self.cfg.get("atr_target_multiplier", 1.25)),
            )
        return min(stop_pct, 3.0), min(target_pct, 3.0)

    def open_trade(
        self,
        symbol: str,
        price: float,
        strategy: str,
        api: Any = None,
        atr: Optional[float] = None,
    ) -> Optional[Position]:
        ok, reason = self.can_open(symbol)
        if not ok:
            self._log(f"[SKIP {symbol}] {reason}")
            return None

        amount = float(self.cfg.get("trade_amount_usdt", 25.0))
        fee_pct = float(self.cfg.get("fee_pct", 0.1))
        mode = self.mode
        if mode == "live":
            if api is None:
                self._log(f"[ERR] API client missing for live buy {symbol}")
                return None
            try:
                order = api.place_market_buy(symbol, amount)
                entry = float(order.get("avg_price") or 0.0)
                qty = float(order.get("filled_qty") or 0.0)
                invested = float(order.get("cost") or 0.0)
                if entry <= 0 or qty <= 0 or invested <= 0:
                    self._log(f"[LIVE BUY NOT FILLED {symbol}] No confirmed executed quantity")
                    return None
                order_id = str(order.get("order_id", ""))
                try:
                    self.live_balance = api.fetch_usdt_balance()
                except Exception:
                    pass
            except Exception as exc:
                self._log(f"[LIVE BUY ERR {symbol}] {exc}")
                return None
        else:
            market_price = float(price)
            slippage_pct = float(self.cfg.get("slippage_pct", 0.05))
            if market_price <= 0:
                return None
            entry = market_price * (1.0 + slippage_pct / 100.0)
            qty = amount * (1.0 - fee_pct / 100.0) / entry
            invested = amount
            self.paper_balance -= amount
            order_id = f"PAPER-{int(time.time())}"

        stop_pct, target_pct = self._exit_distances(entry, atr)
        stop_loss = entry * (1.0 - stop_pct / 100.0)
        take_profit = entry * (1.0 + target_pct / 100.0)
        position = Position(
            symbol=symbol,
            mode=mode,
            entry_price=entry,
            qty=qty,
            invested_usdt=invested,
            stop_loss=stop_loss,
            take_profit=take_profit,
            highest_price=entry,
            current_price=entry,
            strategy=strategy,
            order_id=order_id,
            atr_at_entry=float(atr or 0.0),
        )
        self.positions[symbol] = position
        self._log(
            f"[BUY {mode.upper()}] {symbol} @ {entry:.8g} | {invested:.2f} USDT"
            f" | TP {take_profit:.8g} | SL {stop_loss:.8g}"
        )
        return position

    def evaluate_price(
        self,
        symbol: str,
        live_price: float,
        api: Any = None,
        now_ts: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        position = self.positions.get(symbol)
        if position is None or live_price <= 0:
            return None
        now_value = time.time() if now_ts is None else float(now_ts)
        position.current_price = live_price
        position.highest_price = max(position.highest_price, live_price)

        gain_pct = (position.highest_price / position.entry_price - 1.0) * 100.0
        fee_pct = float(self.cfg.get("fee_pct", 0.1))
        slippage_pct = float(self.cfg.get("slippage_pct", 0.05))
        break_even_activation = float(self.cfg.get("break_even_activation_pct", 0.5))
        if gain_pct >= break_even_activation:
            fee_buffer = 2.0 * (fee_pct + slippage_pct) / 100.0
            break_even = position.entry_price * (1.0 + fee_buffer)
            position.stop_loss = max(position.stop_loss, break_even)

        if bool(self.cfg.get("trailing_stop_enabled", True)):
            activation = float(self.cfg.get("trailing_activation_pct", 0.75))
            distance = float(self.cfg.get("trailing_distance_pct", 0.3))
            if gain_pct >= activation:
                position.trailing_active = True
                new_stop = position.highest_price * (1.0 - distance / 100.0)
                position.stop_loss = max(position.stop_loss, new_stop)

        if live_price <= position.stop_loss:
            return self.close_trade(
                symbol, live_price,
                "TRAIL_SL" if position.trailing_active else "SL", api=api,
            )
        if live_price >= position.take_profit:
            return self.close_trade(symbol, live_price, "TP", api=api)

        max_minutes = float(self.cfg.get("max_holding_minutes", 15))
        if max_minutes > 0 and now_value - position.opened_ts >= max_minutes * 60.0:
            return self.close_trade(symbol, live_price, "TIME_EXIT", api=api)
        return None

    def close_trade(
        self,
        symbol: str,
        exit_price: float,
        reason: str = "MANUAL",
        api: Any = None,
    ) -> Optional[Dict[str, Any]]:
        position = self.positions.get(symbol)
        if position is None:
            return None

        fee_pct = float(self.cfg.get("fee_pct", 0.1))
        if position.mode == "live":
            if api is None:
                self._log(f"[ERR] API client missing for live sell {symbol}")
                return None
            try:
                order = api.place_market_sell(symbol, position.qty)
                actual_exit = float(order.get("avg_price") or 0.0)
                sold_qty = float(order.get("filled_qty") or 0.0)
                proceeds = float(order.get("cost") or 0.0)
                if actual_exit <= 0 or sold_qty <= 0 or proceeds <= 0:
                    self._log(f"[LIVE SELL NOT FILLED {symbol}] Position retained for retry")
                    return None
                try:
                    self.live_balance = api.fetch_usdt_balance()
                except Exception:
                    pass
            except Exception as exc:
                self._log(f"[LIVE SELL ERR {symbol}] {exc}")
                return None
            sold_qty = min(sold_qty, position.qty)
        else:
            market_exit = float(exit_price)
            slippage_pct = float(self.cfg.get("slippage_pct", 0.05))
            if market_exit <= 0:
                return None
            actual_exit = market_exit * (1.0 - slippage_pct / 100.0)
            sold_qty = position.qty
            proceeds = max(0.0, sold_qty * actual_exit * (1.0 - fee_pct / 100.0))
            self.paper_balance += proceeds

        fraction = min(1.0, sold_qty / position.qty) if position.qty > 0 else 0.0
        cost_basis = position.invested_usdt * fraction
        pnl = proceeds - cost_basis
        pnl_pct = (pnl / cost_basis * 100.0) if cost_basis > 0 else 0.0
        self.realized_pnl += pnl

        remaining_qty = max(0.0, position.qty - sold_qty)
        if remaining_qty > max(position.qty * 1e-8, 1e-12):
            position.qty = remaining_qty
            position.invested_usdt = max(0.0, position.invested_usdt - cost_basis)
            self._log(f"[PARTIAL SELL {symbol}] Sold {sold_qty:.8g}; remaining {position.qty:.8g}")
        else:
            del self.positions[symbol]
            if pnl >= 0:
                self.wins += 1
            else:
                self.losses += 1

        record = {
            "closed_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "mode": position.mode,
            "symbol": symbol,
            "entry": position.entry_price,
            "exit": actual_exit,
            "pnl_usdt": pnl,
            "pnl_pct": pnl_pct,
            "reason": reason,
        }
        self.closed_trades.insert(0, record)
        with open(self.trades_csv, "a", newline="", encoding="utf-8-sig") as handle:
            csv.writer(handle).writerow([
                record["closed_at"], record["mode"].upper(), symbol,
                f"{position.entry_price:.8g}", f"{actual_exit:.8g}",
                f"{pnl:.4f}", f"{pnl_pct:.2f}%", reason,
            ])

        sign = "+" if pnl >= 0 else ""
        self._log(
            f"[CLOSE {reason}] {symbol} @ {actual_exit:.8g} | "
            f"PnL: {sign}{pnl:.2f} USDT ({sign}{pnl_pct:.2f}%)"
        )
        return record
