import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.gateio_api import GateioSpotAPI
from core.trader import TradeManager
from indicators.pure_ta import (
    analyze_volume,
    calculate_atr,
    calculate_daily_vwap,
    calculate_rsi,
    compute_ema_signals,
)
from settings import DEFAULT_SETTINGS, load_settings
from strategies.engine import (
    check_htf_15m_trend,
    check_htf_1h_trend,
    evaluate_ema_vwap_vol,
    evaluate_momentum,
)


def make_candles(n=260, start=100.0, step=0.03, volume=1000.0):
    rows = []
    base_ts = 1763942400
    for i in range(n):
        close = start + i * step
        rows.append({
            "ts": base_ts + i * 60,
            "open": close - 0.02,
            "high": close + 0.05,
            "low": close - 0.05,
            "close": close,
            "volume": volume,
            "quote_volume": volume,
        })
    return rows


class FakeLiveAPI:
    def __init__(self):
        self.sell_qty = 5.0
        self.buy_called = False

    def place_market_buy(self, symbol, amount):
        self.buy_called = True
        return {
            "avg_price": 10.0,
            "filled_qty": 10.0,
            "cost": 100.0,
            "order_id": "BUY-1",
        }

    def place_market_sell(self, symbol, qty):
        sold = min(self.sell_qty, qty)
        return {
            "avg_price": 11.0,
            "filled_qty": sold,
            "cost": sold * 11.0,
            "order_id": "SELL-1",
        }

    def fetch_usdt_balance(self):
        return 900.0


class TestGateioMobileEngine(unittest.TestCase):
    def test_settings_creation_and_safe_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = load_settings(td)
            self.assertEqual(cfg["trading"]["mode"], "paper")
            self.assertFalse(cfg["trading"]["live_trading_approved"])
            self.assertEqual(cfg["timeframe"], "5m")
            self.assertEqual(cfg["long_timeframe"], "15m")
            self.assertEqual(cfg["very_long_timeframe"], "1h")
            self.assertEqual(cfg["num_coins"], 150)

    def test_gateio_pair_filter_and_candle_parser(self):
        stables = DEFAULT_SETTINGS["excluded_stables"]
        self.assertTrue(GateioSpotAPI.is_valid_spot_pair("BTC_USDT", stables))
        self.assertFalse(GateioSpotAPI.is_valid_spot_pair("USDC_USDT", stables))
        self.assertFalse(GateioSpotAPI.is_valid_spot_pair("BTC3L_USDT", stables))

        raw = [["1763942400", "50000", "105.0", "106.0", "100.0", "101.0", "500.0", "true"]]
        parsed = GateioSpotAPI.parse_candles(raw)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["close"], 105.0)
        self.assertEqual(parsed[0]["volume"], 50000.0)
        self.assertEqual(parsed[0]["base_volume"], 500.0)
        self.assertTrue(parsed[0]["window_closed"])

        # Also accept the 7-column public example where column 6 is window_closed.
        short_row = [["1763942400", "50000", "105.0", "106.0", "100.0", "101.0", "true"]]
        short_parsed = GateioSpotAPI.parse_candles(short_row)
        self.assertEqual(len(short_parsed), 1)
        self.assertEqual(short_parsed[0]["volume"], 50000.0)
        self.assertTrue(short_parsed[0]["window_closed"])

    def test_hmac_sha512_signature(self):
        signature = GateioSpotAPI.build_signature(
            "GET", "/api/v4/spot/accounts", "currency=USDT", "",
            "1700000000", "mysecret",
        )
        self.assertEqual(len(signature), 128)

    def test_indicators_use_wilder_rsi_and_atr(self):
        rising = [float(i) for i in range(1, 31)]
        rsi = calculate_rsi(rising, 14)
        self.assertIsNotNone(rsi)
        self.assertAlmostEqual(rsi[14], 100.0)
        self.assertAlmostEqual(rsi[-1], 100.0)

        candles = make_candles(40)
        atr = calculate_atr(candles, 14)
        self.assertIsNotNone(atr)
        self.assertGreater(atr[14], 0.0)
        self.assertTrue(all(value >= 0 for value in atr))

    def test_volume_uses_current_closed_candle(self):
        candles = make_candles(40, volume=1000.0)
        for index, candle in enumerate(candles[:-1]):
            candle["volume"] = 1000.0 + (index % 5) * 20.0
        candles[-1]["volume"] = 5000.0
        result = analyze_volume(
            candles, period=20, multiplier=2.0, min_z_score=1.0,
            idx=len(candles) - 1, closed_only=False,
        )
        self.assertTrue(result["is_surge"])
        self.assertTrue(result["is_z_surge"])

    def test_strategy_returns_diagnostics_without_lookahead(self):
        candles = make_candles(260)
        cfg = deepcopy(DEFAULT_SETTINGS)
        cfg["use_closed_candles_only"] = False
        result = evaluate_ema_vwap_vol(candles, cfg)
        self.assertIn("signal", result)
        self.assertIn("entry_trigger", result)
        self.assertIn("checks", result)
        self.assertIn("volume_confirmation", result["checks"])
        # A smooth trend without a valid volume/candle setup is not bought.
        self.assertFalse(result["signal"])
        momentum = evaluate_momentum(candles, cfg)
        self.assertIn("signal", momentum)

    def test_daily_vwap_and_higher_timeframe_filters(self):
        candles = make_candles(260, start=10.0, step=0.2)
        self.assertIsNotNone(calculate_daily_vwap(candles, closed_only=False))
        self.assertTrue(check_htf_15m_trend(candles, closed_only=False))
        self.assertTrue(check_htf_1h_trend(candles, closed_only=False))

    def test_paper_trader_trailing_and_limits(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = deepcopy(DEFAULT_SETTINGS)
            cfg["trading"].update({
                "paper_balance_usdt": 100.0,
                "trade_amount_usdt": 50.0,
                "max_open_trades": 1,
                "take_profit_pct": 2.0,
                "stop_loss_pct": 1.0,
                "trailing_stop_enabled": True,
                "trailing_activation_pct": 1.0,
                "trailing_distance_pct": 0.5,
                "break_even_activation_pct": 5.0,
                "fee_pct": 0.0,
                "slippage_pct": 0.0,
            })
            trader = TradeManager(cfg, data_dir=td)
            self.assertIsNotNone(trader.open_trade("BTC_USDT", 100.0, "EMA_VWAP_VOL"))
            self.assertAlmostEqual(trader.positions["BTC_USDT"].entry_price, 100.0)
            self.assertIsNone(trader.open_trade("ETH_USDT", 200.0, "EMA_VWAP_VOL"))
            self.assertIsNone(trader.evaluate_price("BTC_USDT", 101.5))
            self.assertTrue(trader.positions["BTC_USDT"].trailing_active)
            closed = trader.evaluate_price("BTC_USDT", 100.8)
            self.assertIsNotNone(closed)
            self.assertEqual(closed["reason"], "TRAIL_SL")
            self.assertGreater(trader.realized_pnl, 0.0)

    def test_atr_exits_and_time_exit(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = deepcopy(DEFAULT_SETTINGS)
            cfg["trading"]["fee_pct"] = 0.0
            cfg["trading"]["trailing_stop_enabled"] = False
            cfg["trading"]["break_even_activation_pct"] = 5.0
            cfg["trading"]["max_holding_minutes"] = 15
            cfg["trading"]["slippage_pct"] = 0.05
            trader = TradeManager(cfg, data_dir=td)
            position = trader.open_trade("BTC_USDT", 100.0, "EMA", atr=1.0)
            self.assertAlmostEqual(position.entry_price, 100.05)
            self.assertAlmostEqual(position.take_profit, 101.3, places=6)
            self.assertAlmostEqual(position.stop_loss, 98.45, places=6)
            position.opened_ts = 100.0
            record = trader.evaluate_price("BTC_USDT", 100.0, now_ts=100.0 + 901.0)
            self.assertEqual(record["reason"], "TIME_EXIT")

    def test_live_mode_is_locked_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = deepcopy(DEFAULT_SETTINGS)
            cfg["trading"]["mode"] = "live"
            api = FakeLiveAPI()
            trader = TradeManager(cfg, data_dir=td)
            allowed, reason = trader.can_open("BTC_USDT")
            self.assertFalse(allowed)
            self.assertIn("locked", reason.lower())
            self.assertIsNone(trader.open_trade("BTC_USDT", 10.0, "EMA", api=api))
            self.assertFalse(api.buy_called)

    def test_partial_live_sell_does_not_forget_remaining_quantity(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = deepcopy(DEFAULT_SETTINGS)
            cfg["trading"].update({
                "mode": "live",
                "live_trading_approved": True,
                "fee_pct": 0.0,
            })
            api = FakeLiveAPI()
            trader = TradeManager(cfg, data_dir=td)
            position = trader.open_trade("BTC_USDT", 10.0, "EMA", api=api)
            self.assertIsNotNone(position)
            partial = trader.close_trade("BTC_USDT", 11.0, "MANUAL", api=api)
            self.assertIsNotNone(partial)
            self.assertIn("BTC_USDT", trader.positions)
            self.assertAlmostEqual(trader.positions["BTC_USDT"].qty, 5.0)
            api.sell_qty = 5.0
            trader.close_trade("BTC_USDT", 11.0, "MANUAL", api=api)
            self.assertNotIn("BTC_USDT", trader.positions)


if __name__ == "__main__":
    unittest.main(verbosity=2)
