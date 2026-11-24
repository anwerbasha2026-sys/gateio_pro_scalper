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
from indicators.pure_ta import analyze_volume, calculate_daily_vwap, calculate_rsi, compute_ema_signals
from settings import DEFAULT_SETTINGS, load_settings
from strategies.engine import check_htf_15m_trend, check_htf_1h_trend, evaluate_ema_vwap_vol, evaluate_momentum


def make_bullish_candles(n: int = 260, spike_vol: float = 5000.0):
    start_sec = 1763942400
    step = 300
    out = []
    for i in range(n):
        ts = start_sec + i * step
        if i < 210:
            price = 100.0 + i * 0.15
        elif i < n - 3:
            price = 131.5 - (i - 209) * 0.25
        elif i == n - 3:
            price = 123.0
        elif i == n - 2:
            price = 135.0
        else:
            price = 135.5
        vol = spike_vol if i == n - 2 else 1000.0 + (i % 5) * 20.0
        out.append({"ts": ts, "open": price - 0.3, "high": price + 0.5, "low": price - 0.5, "close": price, "volume": vol})
    return out


class TestGateioMobileEngine(unittest.TestCase):
    def test_settings_creation(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = load_settings(td)
            self.assertEqual(cfg["trading"]["mode"], "paper")
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
        self.assertEqual(parsed[0]["volume"], 500.0)

    def test_hmac_sha512_signature(self):
        sig = GateioSpotAPI.build_signature("GET", "/api/v4/spot/accounts", "currency=USDT", "", "1700000000", "mysecret")
        self.assertEqual(len(sig), 128)  # SHA-512 hex digest

    def test_pure_ta_indicators_and_strategy(self):
        candles = make_bullish_candles(260, spike_vol=5000.0)
        ema_res = compute_ema_signals(candles, use_ema200=True, closed_only=True)
        self.assertIsNotNone(ema_res)
        self.assertIn(ema_res["score"], (90, 100))

        vwap = calculate_daily_vwap(candles, closed_only=True)
        self.assertIsNotNone(vwap)

        vol = analyze_volume(candles, period=20, multiplier=2.0, min_z_score=1.5, closed_only=True)
        self.assertTrue(vol["is_surge"])
        self.assertTrue(vol["is_z_surge"])

        cfg = deepcopy(DEFAULT_SETTINGS)
        cfg["use_z_score"] = True
        evv = evaluate_ema_vwap_vol(candles, cfg)
        self.assertTrue(evv["signal"])

        rsi = calculate_rsi([c["close"] for c in candles], 14)
        self.assertTrue(0.0 <= rsi[-2] <= 100.0)
        mom = evaluate_momentum(candles, cfg)
        self.assertIn("signal", mom)

    def test_htf_filters(self):
        start_sec = 1763942400
        candles = [
            {"ts": start_sec + i * 900, "open": 10 + i * 0.2, "high": 10.5 + i * 0.2, "low": 9.8 + i * 0.2, "close": 10.2 + i * 0.2, "volume": 1000.0}
            for i in range(230)
        ]
        self.assertTrue(check_htf_15m_trend(candles))
        self.assertTrue(check_htf_1h_trend(candles))

    def test_paper_trader_trailing_and_limits(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = deepcopy(DEFAULT_SETTINGS)
            cfg["trading"]["paper_balance_usdt"] = 100.0
            cfg["trading"]["trade_amount_usdt"] = 50.0
            cfg["trading"]["max_open_trades"] = 1
            cfg["trading"]["take_profit_pct"] = 2.0
            cfg["trading"]["stop_loss_pct"] = 1.0
            cfg["trading"]["trailing_stop_enabled"] = True
            cfg["trading"]["trailing_activation_pct"] = 1.0
            cfg["trading"]["trailing_distance_pct"] = 0.5
            cfg["trading"]["fee_pct"] = 0.0

            tm = TradeManager(cfg, data_dir=td)
            self.assertIsNotNone(tm.open_trade("BTC_USDT", 100.0, "EMA_VWAP_VOL"))
            self.assertIsNone(tm.open_trade("ETH_USDT", 200.0, "EMA_VWAP_VOL"))
            self.assertIsNone(tm.evaluate_price("BTC_USDT", 101.5))
            self.assertTrue(tm.positions["BTC_USDT"].trailing_active)
            closed = tm.evaluate_price("BTC_USDT", 100.8)
            self.assertIsNotNone(closed)
            self.assertEqual(closed["reason"], "TRAIL_SL")
            self.assertGreater(tm.realized_pnl, 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
