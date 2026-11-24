import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, List, Optional

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.switch import Switch
from kivy.uix.tabbedpanel import TabbedPanel, TabbedPanelItem
from kivy.uix.textinput import TextInput

from core.gateio_api import GateioSpotAPI
from core.notifier import SignalNotifier
from core.trader import TradeManager
from settings import load_settings, save_settings
from strategies.engine import scan_symbol

Window.clearcolor = (0.06, 0.09, 0.16, 1)
TIMEFRAMES = ("1m", "5m", "15m", "30m", "1h", "4h", "8h", "1d")


def make_input(val: str = "", password: bool = False) -> TextInput:
    return TextInput(
        text=str(val),
        multiline=False,
        password=password,
        size_hint_y=None,
        height=dp(38),
        background_color=(0.08, 0.12, 0.22, 1),
        foreground_color=(0.95, 0.97, 1.0, 1),
        cursor_color=(0.22, 0.74, 0.97, 1),
        padding=(dp(8), dp(8)),
    )


def make_row(label_text: str, widget: Any) -> BoxLayout:
    row = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(42), spacing=dp(8))
    lbl = Label(
        text=label_text,
        halign="left",
        valign="middle",
        color=(0.88, 0.92, 0.98, 1),
        size_hint_x=0.58,
    )
    lbl.bind(size=lbl.setter("text_size"))
    widget.size_hint_x = 0.42
    row.add_widget(lbl)
    row.add_widget(widget)
    return row


class GateioScalperMobileApp(App):
    title = "Gate.io Spot Pro Scalper"

    def build(self):
        self.settings = load_settings(self.user_data_dir)
        self.api = GateioSpotAPI(
            api_key=self.settings["gateio_api"].get("api_key", ""),
            api_secret=self.settings["gateio_api"].get("api_secret", ""),
        )
        self.notifier = SignalNotifier(self.settings, data_dir=self.user_data_dir)
        self.trader = TradeManager(self.settings, data_dir=self.user_data_dir, log_cb=self._enqueue_log)

        self._stop_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._running = False
        self._manual_closes: "queue.Queue[str]" = queue.Queue()
        self._log_queue: "queue.Queue[str]" = queue.Queue()
        self._log_lines: List[str] = []

        root = BoxLayout(orientation="vertical", padding=dp(8), spacing=dp(6))

        # Top Action Bar
        top = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(46), spacing=dp(6))
        self.lbl_mode = Button(
            text="PAPER",
            size_hint_x=0.22,
            background_normal="",
            background_color=(0.13, 0.77, 0.37, 1),
            color=(0.02, 0.15, 0.07, 1),
            bold=True,
        )
        self.btn_start = Button(
            text="START",
            size_hint_x=0.26,
            background_normal="",
            background_color=(0.13, 0.77, 0.37, 1),
            color=(0.02, 0.15, 0.07, 1),
            bold=True,
            on_release=lambda *_: self._start_bot(),
        )
        self.btn_stop = Button(
            text="STOP",
            size_hint_x=0.24,
            background_normal="",
            background_color=(0.93, 0.27, 0.27, 1),
            disabled=True,
            bold=True,
            on_release=lambda *_: self._stop_bot(),
        )
        self.btn_save = Button(
            text="SAVE",
            size_hint_x=0.28,
            background_normal="",
            background_color=(0.15, 0.39, 0.92, 1),
            bold=True,
            on_release=lambda *_: self._save_and_apply(),
        )
        for w in (self.lbl_mode, self.btn_start, self.btn_stop, self.btn_save):
            top.add_widget(w)
        root.add_widget(top)

        # KPI Grid
        kpi = GridLayout(cols=2, size_hint_y=None, height=dp(74), spacing=dp(4))
        self.kpi_bal = Label(text="Balance: 1000.00 USDT", bold=True, color=(0.22, 0.74, 0.97, 1))
        self.kpi_open = Label(text="Open Trades: 0 / 3", bold=True)
        self.kpi_pnl = Label(text="Realized PnL: 0.00 USDT", bold=True)
        self.kpi_status = Label(text="Status: Stopped", color=(0.6, 0.67, 0.75, 1))
        for w in (self.kpi_bal, self.kpi_open, self.kpi_pnl, self.kpi_status):
            kpi.add_widget(w)
        root.add_widget(kpi)

        # Tabs
        tabs = TabbedPanel(do_default_tab=False, tab_height=dp(40))
        tabs.add_widget(self._build_dash_tab())
        tabs.add_widget(self._build_strategy_tab())
        tabs.add_widget(self._build_trading_tab())
        tabs.add_widget(self._build_api_tab())
        tabs.add_widget(self._build_history_tab())
        root.add_widget(tabs)

        self._refresh_kpis()
        Clock.schedule_interval(self._ui_tick, 0.4)
        return root

    def _build_dash_tab(self) -> TabbedPanelItem:
        tab = TabbedPanelItem(text="Dashboard")
        box = BoxLayout(orientation="vertical", padding=dp(6), spacing=dp(6))

        hdr = BoxLayout(size_hint_y=None, height=dp(38), spacing=dp(6))
        hdr.add_widget(Label(text="Open Positions", bold=True, color=(0.22, 0.74, 0.97, 1)))
        hdr.add_widget(
            Button(
                text="Close ALL",
                size_hint_x=0.35,
                background_normal="",
                background_color=(0.93, 0.27, 0.27, 1),
                on_release=lambda *_: self._close_all(),
            )
        )
        box.add_widget(hdr)

        pos_scroll = ScrollView(size_hint_y=0.45)
        self.pos_container = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(4))
        self.pos_container.bind(minimum_height=self.pos_container.setter("height"))
        pos_scroll.add_widget(self.pos_container)
        box.add_widget(pos_scroll)

        box.add_widget(Label(text="Live Signals & Activity Log", size_hint_y=None, height=dp(24), bold=True, color=(0.22, 0.74, 0.97, 1)))
        log_scroll = ScrollView(size_hint_y=0.55)
        self.lbl_log = Label(
            text="Ready. Tap START to scan Gate.io Spot.\n",
            size_hint_y=None,
            halign="left",
            valign="top",
            font_size="12sp",
            color=(0.88, 0.92, 0.98, 1),
        )
        self.lbl_log.bind(width=lambda inst, w: setattr(inst, "text_size", (w - dp(12), None)))
        self.lbl_log.bind(texture_size=lambda inst, ts: setattr(inst, "height", max(ts[1], dp(150))))
        log_scroll.add_widget(self.lbl_log)
        box.add_widget(log_scroll)

        tab.add_widget(box)
        return tab

    def _build_strategy_tab(self) -> TabbedPanelItem:
        tab = TabbedPanelItem(text="Strategy")
        scroll = ScrollView()
        form = BoxLayout(orientation="vertical", size_hint_y=None, padding=dp(8), spacing=dp(6))
        form.bind(minimum_height=form.setter("height"))

        s = self.settings
        self.sw_evv = Switch(active=bool(s["enable_ema_vwap_vol"]))
        self.sw_mom = Switch(active=bool(s["enable_momentum"]))
        self.sw_ema200 = Switch(active=bool(s["use_ema200"]))
        self.sw_htf = Switch(active=bool(s["use_htf_filter"]))
        self.sw_vlong = Switch(active=bool(s["use_very_long_filter"]))
        self.sw_closed = Switch(active=bool(s["use_closed_candles_only"]))
        self.sw_z = Switch(active=bool(s["use_z_score"]))

        self.in_num_coins = make_input(s["num_coins"])
        self.in_min_vol = make_input(s["min_quote_volume_usdt"])
        self.sp_tf = Spinner(text=s["timeframe"], values=TIMEFRAMES, size_hint_y=None, height=dp(38))
        self.sp_htf1 = Spinner(text=s["long_timeframe"], values=TIMEFRAMES, size_hint_y=None, height=dp(38))
        self.sp_htf2 = Spinner(text=s["very_long_timeframe"], values=TIMEFRAMES, size_hint_y=None, height=dp(38))
        self.in_vol_mult = make_input(s["volume_multiplier"])
        self.in_vol_per = make_input(s["volume_period"])
        self.in_min_z = make_input(s["min_z_score"])
        self.in_lookback = make_input(s["crossover_lookback"])
        self.in_interval = make_input(s["scan_interval_sec"])
        self.in_workers = make_input(s["max_workers"])
        self.in_cooldown = make_input(s["signal_cooldown_min"])

        rows = [
            ("EMA + Daily VWAP + Vol", self.sw_evv),
            ("Momentum (RSI+EMA+VWAP)", self.sw_mom),
            ("Use EMA 200 Filter", self.sw_ema200),
            ("Confirm 15m HTF Trend", self.sw_htf),
            ("Confirm 1h HTF Trend", self.sw_vlong),
            ("Closed Candles Only", self.sw_closed),
            ("Enable Volume Z-Score", self.sw_z),
            ("Top Coins to Scan", self.in_num_coins),
            ("Min 24h Volume (USDT)", self.in_min_vol),
            ("Entry Timeframe", self.sp_tf),
            ("Medium Timeframe (HTF1)", self.sp_htf1),
            ("Large Timeframe (HTF2)", self.sp_htf2),
            ("Volume Surge Multiplier", self.in_vol_mult),
            ("Volume Avg Period", self.in_vol_per),
            ("Min Volume Z-Score", self.in_min_z),
            ("Crossover Lookback", self.in_lookback),
            ("Scan Interval (sec)", self.in_interval),
            ("Parallel Workers", self.in_workers),
            ("Alert Cooldown (min)", self.in_cooldown),
        ]
        for lbl, w in rows:
            form.add_widget(make_row(lbl, w))

        scroll.add_widget(form)
        tab.add_widget(scroll)
        return tab

    def _build_trading_tab(self) -> TabbedPanelItem:
        tab = TabbedPanelItem(text="Trading")
        scroll = ScrollView()
        form = BoxLayout(orientation="vertical", size_hint_y=None, padding=dp(8), spacing=dp(6))
        form.bind(minimum_height=form.setter("height"))

        t = self.settings["trading"]
        self.sp_mode = Spinner(text=t["mode"].upper(), values=("PAPER", "LIVE"), size_hint_y=None, height=dp(38))
        self.sw_auto = Switch(active=bool(t["auto_trade"]))
        self.in_amt = make_input(t["trade_amount_usdt"])
        self.in_max_t = make_input(t["max_open_trades"])
        self.in_paper_bal = make_input(t["paper_balance_usdt"])
        self.in_tp = make_input(t["take_profit_pct"])
        self.in_sl = make_input(t["stop_loss_pct"])
        self.sw_trail = Switch(active=bool(t["trailing_stop_enabled"]))
        self.in_trail_act = make_input(t["trailing_activation_pct"])
        self.in_trail_dist = make_input(t["trailing_distance_pct"])
        self.in_fee = make_input(t["fee_pct"])

        for lbl, w in [
            ("Account Mode (PAPER/LIVE)", self.sp_mode),
            ("Auto Open Trades", self.sw_auto),
            ("Trade Amount (USDT)", self.in_amt),
            ("Max Open Trades", self.in_max_t),
            ("Initial Paper Balance", self.in_paper_bal),
            ("Take Profit (%)", self.in_tp),
            ("Stop Loss (%)", self.in_sl),
            ("Trailing Stop Loss", self.sw_trail),
            ("Trailing Activation (%)", self.in_trail_act),
            ("Trailing Distance (%)", self.in_trail_dist),
            ("Paper Fee (%)", self.in_fee),
        ]:
            form.add_widget(make_row(lbl, w))

        form.add_widget(
            Button(
                text="Reset Paper Balance",
                size_hint_y=None,
                height=dp(42),
                background_normal="",
                background_color=(0.2, 0.25, 0.35, 1),
                on_release=lambda *_: self._reset_paper(),
            )
        )
        scroll.add_widget(form)
        tab.add_widget(scroll)
        return tab

    def _build_api_tab(self) -> TabbedPanelItem:
        tab = TabbedPanelItem(text="API / TG")
        scroll = ScrollView()
        form = BoxLayout(orientation="vertical", size_hint_y=None, padding=dp(8), spacing=dp(8))
        form.bind(minimum_height=form.setter("height"))

        api = self.settings["gateio_api"]
        tg = self.settings["telegram"]
        self.in_key = make_input(api.get("api_key", ""), password=True)
        self.in_sec = make_input(api.get("api_secret", ""), password=True)
        self.sw_tg = Switch(active=bool(tg.get("enabled", False)))
        self.in_tg_tok = make_input(tg.get("bot_token", ""))
        self.in_tg_chat = make_input(tg.get("chat_id", ""))

        for lbl, w in [
            ("Gate.io API Key", self.in_key),
            ("Gate.io API Secret", self.in_sec),
            ("Enable Telegram", self.sw_tg),
            ("Telegram Bot Token", self.in_tg_tok),
            ("Telegram Chat ID", self.in_tg_chat),
        ]:
            form.add_widget(make_row(lbl, w))

        form.add_widget(
            Button(
                text="Test Telegram",
                size_hint_y=None,
                height=dp(42),
                background_normal="",
                background_color=(0.15, 0.39, 0.92, 1),
                on_release=lambda *_: self._test_tg(),
            )
        )
        scroll.add_widget(form)
        tab.add_widget(scroll)
        return tab

    def _build_history_tab(self) -> TabbedPanelItem:
        tab = TabbedPanelItem(text="History")
        scroll = ScrollView()
        self.hist_container = BoxLayout(orientation="vertical", size_hint_y=None, padding=dp(8), spacing=dp(4))
        self.hist_container.bind(minimum_height=self.hist_container.setter("height"))
        scroll.add_widget(self.hist_container)
        tab.add_widget(scroll)
        return tab

    def _show_popup(self, title: str, msg: str) -> None:
        box = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(10))
        lbl = Label(text=msg, halign="center", valign="middle")
        lbl.bind(size=lbl.setter("text_size"))
        btn = Button(text="OK", size_hint_y=None, height=dp(40))
        box.add_widget(lbl)
        box.add_widget(btn)
        pop = Popup(title=title, content=box, size_hint=(0.85, 0.35))
        btn.bind(on_release=pop.dismiss)
        pop.open()

    def _save_and_apply(self) -> bool:
        try:
            cfg = deepcopy(self.settings)
            cfg["enable_ema_vwap_vol"] = bool(self.sw_evv.active)
            cfg["enable_momentum"] = bool(self.sw_mom.active)
            cfg["use_ema200"] = bool(self.sw_ema200.active)
            cfg["use_htf_filter"] = bool(self.sw_htf.active)
            cfg["use_very_long_filter"] = bool(self.sw_vlong.active)
            cfg["use_closed_candles_only"] = bool(self.sw_closed.active)
            cfg["use_z_score"] = bool(self.sw_z.active)
            cfg["num_coins"] = max(1, int(self.in_num_coins.text))
            cfg["min_quote_volume_usdt"] = max(0.0, float(self.in_min_vol.text))
            cfg["timeframe"] = self.sp_tf.text
            cfg["long_timeframe"] = self.sp_htf1.text
            cfg["very_long_timeframe"] = self.sp_htf2.text
            cfg["volume_multiplier"] = max(0.5, float(self.in_vol_mult.text))
            cfg["volume_period"] = max(5, int(self.in_vol_per.text))
            cfg["min_z_score"] = float(self.in_min_z.text)
            cfg["crossover_lookback"] = max(1, int(self.in_lookback.text))
            cfg["scan_interval_sec"] = max(3, int(self.in_interval.text))
            cfg["max_workers"] = max(1, min(16, int(self.in_workers.text)))
            cfg["signal_cooldown_min"] = max(0, int(self.in_cooldown.text))

            mode = self.sp_mode.text.lower()
            cfg["trading"]["mode"] = mode
            cfg["trading"]["auto_trade"] = bool(self.sw_auto.active)
            cfg["trading"]["trade_amount_usdt"] = max(1.0, float(self.in_amt.text))
            cfg["trading"]["max_open_trades"] = max(1, int(self.in_max_t.text))
            cfg["trading"]["paper_balance_usdt"] = max(1.0, float(self.in_paper_bal.text))
            cfg["trading"]["take_profit_pct"] = max(0.1, float(self.in_tp.text))
            cfg["trading"]["stop_loss_pct"] = max(0.1, float(self.in_sl.text))
            cfg["trading"]["trailing_stop_enabled"] = bool(self.sw_trail.active)
            cfg["trading"]["trailing_activation_pct"] = max(0.05, float(self.in_trail_act.text))
            cfg["trading"]["trailing_distance_pct"] = max(0.05, float(self.in_trail_dist.text))
            cfg["trading"]["fee_pct"] = max(0.0, float(self.in_fee.text))

            cfg["gateio_api"]["api_key"] = self.in_key.text.strip()
            cfg["gateio_api"]["api_secret"] = self.in_sec.text.strip()
            cfg["telegram"]["enabled"] = bool(self.sw_tg.active)
            cfg["telegram"]["bot_token"] = self.in_tg_tok.text.strip()
            cfg["telegram"]["chat_id"] = self.in_tg_chat.text.strip()
        except ValueError as e:
            self._show_popup("Invalid Input", f"Check numeric values:\n{e}")
            return False

        if cfg["trading"]["mode"] == "live":
            if not (cfg["gateio_api"]["api_key"] and cfg["gateio_api"]["api_secret"]):
                self._show_popup("API Keys Needed", "Enter Gate.io API Key & Secret in API/TG tab for LIVE mode.")
                return False

        self.settings = cfg
        save_settings(cfg, self.user_data_dir)
        self.api.update_keys(cfg["gateio_api"]["api_key"], cfg["gateio_api"]["api_secret"])
        self.notifier.update_settings(cfg)
        self.trader.update_settings(cfg)
        self._refresh_kpis()
        self._enqueue_log("Settings saved and applied.")
        return True

    def _reset_paper(self) -> None:
        try:
            bal = max(1.0, float(self.in_paper_bal.text))
        except ValueError:
            self._show_popup("Error", "Invalid paper balance.")
            return
        self.trader.reset_paper(bal)
        self._refresh_kpis()
        self._refresh_positions_ui()

    def _test_tg(self) -> None:
        if not self._save_and_apply():
            return
        def _run():
            ok = self.notifier.send_telegram("✅ Test from Gate.io Spot Pro Scalper Mobile!")
            self._enqueue_log("Telegram test sent OK!" if ok else "Telegram test failed.")
        threading.Thread(target=_run, daemon=True).start()

    def _start_bot(self) -> None:
        if self._running or not self._save_and_apply():
            return
        self._stop_event.clear()
        self._running = True
        self.btn_start.disabled = True
        self.btn_stop.disabled = False
        self.kpi_status.text = "Status: Running..."
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

    def _stop_bot(self) -> None:
        self._stop_event.set()
        self._running = False
        self.btn_start.disabled = False
        self.btn_stop.disabled = True
        self.kpi_status.text = "Status: Stopped"
        self._enqueue_log("Stopping scanner...")

    def _close_all(self) -> None:
        for sym in list(self.trader.positions.keys()):
            self._manual_closes.put(sym)

    def _worker_loop(self) -> None:
        cycle = 0
        if self.trader.mode == "live":
            try:
                self.trader.live_balance = self.api.fetch_usdt_balance()
                self._enqueue_log(f"Connected LIVE | Balance: {self.trader.live_balance:.2f} USDT")
            except Exception as e:
                self._enqueue_log(f"[LIVE Balance ERR] {e}")

        while not self._stop_event.is_set():
            cycle += 1
            cfg = self.settings
            t0 = time.perf_counter()

            self._drain_manual_closes()
            self._monitor_positions()

            try:
                pairs = self.api.fetch_top_usdt_pairs(
                    num_coins=int(cfg["num_coins"]),
                    min_quote_volume=float(cfg["min_quote_volume_usdt"]),
                    excluded_stables=cfg["excluded_stables"],
                )
            except Exception as e:
                self._enqueue_log(f"[Cycle {cycle}] Pair fetch error: {e}")
                self._sleep_with_monitor(6)
                continue

            signals = 0
            workers = max(1, int(cfg.get("max_workers", 6)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(scan_symbol, self.api, p, cfg): p for p in pairs}
                for fut in as_completed(futs):
                    if self._stop_event.is_set():
                        break
                    try:
                        res = fut.result()
                    except Exception:
                        res = None
                    if isinstance(res, dict):
                        if self.notifier.dispatch(res):
                            signals += 1
                            self._enqueue_log(
                                f"[SIGNAL] {res['symbol']} ({res['timeframe']}) @ {res['price']:.8g} | Vol {res.get('vol_ratio', 0):.2f}x"
                            )
                            if cfg["trading"].get("auto_trade", True):
                                live_p = self.api.fetch_last_price(res["symbol"]) or res["price"]
                                self.trader.open_trade(res["symbol"], live_p, "+".join(res["strategies"]), api=self.api)

            dt = time.perf_counter() - t0
            self._enqueue_log(f"[Cycle {cycle}] Scanned {len(pairs)} pairs in {dt:.1f}s | Signals: {signals}")
            self._sleep_with_monitor(int(cfg["scan_interval_sec"]))

        self._running = False

    def _drain_manual_closes(self) -> None:
        while not self._manual_closes.empty():
            try:
                sym = self._manual_closes.get_nowait()
            except queue.Empty:
                break
            if sym in self.trader.positions:
                p = self.api.fetch_last_price(sym) or self.trader.positions[sym].current_price
                self.trader.close_trade(sym, p, "MANUAL", api=self.api)

    def _monitor_positions(self) -> None:
        for sym in list(self.trader.positions.keys()):
            if self._stop_event.is_set():
                break
            p = self.api.fetch_last_price(sym)
            if p is not None:
                self.trader.evaluate_price(sym, p, api=self.api)

    def _sleep_with_monitor(self, total_sec: int) -> None:
        mon = max(2, int(self.settings["trading"].get("monitor_interval_sec", 5)))
        waited = 0
        while waited < total_sec and not self._stop_event.is_set():
            time.sleep(1)
            waited += 1
            self._drain_manual_closes()
            if self.trader.positions and waited % mon == 0:
                self._monitor_positions()

    def _enqueue_log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_queue.put(f"[{ts}] {msg}")

    def _ui_tick(self, _dt: float) -> None:
        updated = False
        while not self._log_queue.empty():
            try:
                line = self._log_queue.get_nowait()
                self._log_lines.append(line)
                updated = True
            except queue.Empty:
                break
        if updated:
            self._log_lines = self._log_lines[-120:]
            self.lbl_log.text = "\n".join(self._log_lines)

        self._refresh_kpis()
        self._refresh_positions_ui()
        self._refresh_history_ui()

    def _refresh_kpis(self) -> None:
        mode = self.trader.mode
        if mode == "live":
            self.lbl_mode.text = "LIVE"
            self.lbl_mode.background_color = (0.93, 0.27, 0.27, 1)
            self.lbl_mode.color = (1, 1, 1, 1)
            bal = f"{self.trader.live_balance:.2f}" if self.trader.live_balance is not None else "Connect..."
        else:
            self.lbl_mode.text = "PAPER"
            self.lbl_mode.background_color = (0.13, 0.77, 0.37, 1)
            self.lbl_mode.color = (0.02, 0.15, 0.07, 1)
            bal = f"{self.trader.paper_balance:.2f}"

        max_t = self.settings["trading"].get("max_open_trades", 3)
        self.kpi_bal.text = f"Balance: {bal} USDT"
        self.kpi_open.text = f"Open Trades: {len(self.trader.positions)} / {max_t}"
        pnl = self.trader.realized_pnl
        sign = "+" if pnl > 0 else ""
        self.kpi_pnl.text = f"PnL: {sign}{pnl:.2f} USDT ({self.trader.win_rate:.0f}%)"
        self.kpi_pnl.color = (0.13, 0.77, 0.37, 1) if pnl >= 0 else (0.93, 0.27, 0.27, 1)
        if not self._running:
            self.btn_start.disabled = False
            self.btn_stop.disabled = True
            self.kpi_status.text = "Status: Stopped"

    def _refresh_positions_ui(self) -> None:
        self.pos_container.clear_widgets()
        if not self.trader.positions:
            self.pos_container.add_widget(
                Label(text="No open positions.", size_hint_y=None, height=dp(32), color=(0.6, 0.67, 0.75, 1))
            )
            return
        for pos in list(self.trader.positions.values()):
            row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(4))
            pnl = pos.unrealized_pnl_usdt
            pct = pos.unrealized_pnl_pct
            sign = "+" if pnl >= 0 else ""
            info = Label(
                text=f"{pos.symbol} | In:{pos.entry_price:.6g} Now:{pos.current_price:.6g} | {sign}{pnl:.2f}$ ({sign}{pct:.2f}%)",
                font_size="12sp",
                color=(0.13, 0.77, 0.37, 1) if pnl >= 0 else (0.93, 0.27, 0.27, 1),
            )
            btn_x = Button(
                text="Close",
                size_hint_x=0.22,
                background_normal="",
                background_color=(0.96, 0.62, 0.04, 1),
                color=(0.1, 0.1, 0.1, 1),
                on_release=lambda _b, s=pos.symbol: self._manual_closes.put(s),
            )
            row.add_widget(info)
            row.add_widget(btn_x)
            self.pos_container.add_widget(row)

    def _refresh_history_ui(self) -> None:
        self.hist_container.clear_widgets()
        if not self.trader.closed_trades:
            self.hist_container.add_widget(
                Label(text="No closed trades yet.", size_hint_y=None, height=dp(32), color=(0.6, 0.67, 0.75, 1))
            )
            return
        for rec in self.trader.closed_trades[:40]:
            pnl = rec["pnl_usdt"]
            sign = "+" if pnl >= 0 else ""
            lbl = Label(
                text=f"{rec['symbol']} ({rec['reason']}) | {rec['entry']:.6g}->{rec['exit']:.6g} | {sign}{pnl:.2f}$ ({sign}{rec['pnl_pct']:.2f}%)",
                size_hint_y=None,
                height=dp(30),
                font_size="12sp",
                color=(0.13, 0.77, 0.37, 1) if pnl >= 0 else (0.93, 0.27, 0.27, 1),
            )
            self.hist_container.add_widget(lbl)


if __name__ == "__main__":
    GateioScalperMobileApp().run()
