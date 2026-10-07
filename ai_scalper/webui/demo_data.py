"""Synthetic bot telemetry for `--demo` mode.

Mirrors the exact shapes returned by the live `hbot` commands, so the frontend runs the same code
path whether it is showing a real VPS bot or a local preview. Nothing here touches the network.
"""
import math
import random
import time
from typing import Dict, List, Optional

PAIR = "BTC-USDT"
CONNECTOR = "binance_paper_trade"
CONFIG_FILE = "conf_ai_scalper.yml"

LIVE_FIELDS = ["close_on_stale_signal", "confidence_sizing", "cooldown_time", "long_threshold",
               "max_barrier", "max_executors_per_side", "min_barrier", "min_size_scale",
               "short_threshold", "signal_timeout", "sl_multiplier", "take_profit",
               "take_profit_order_type", "time_limit", "tp_multiplier", "total_amount_quote",
               "trailing_stop", "entry_order_type", "activation_bounds", "stop_loss",
               "manual_kill_switch"]

FIELDS = {
    "id": "ai_scalper_btc_usdt_paper_v1",
    "controller_name": "ai_scalper",
    "controller_type": "directional_trading",
    "connector_name": CONNECTOR,
    "trading_pair": PAIR,
    "total_amount_quote": 100,
    "leverage": 1,
    "position_mode": "ONEWAY",
    "max_executors_per_side": 1,
    "cooldown_time": 45,
    "stop_loss": 0.004,
    "take_profit": 0.003,
    "time_limit": 600,
    "take_profit_order_type": "LIMIT",
    "trailing_stop": None,
    "topic": "hbot/predictions",
    "long_threshold": 0.55,
    "short_threshold": 0.55,
    "signal_timeout": 30,
    "close_on_stale_signal": True,
    "sl_multiplier": 1.5,
    "tp_multiplier": 1.0,
    "min_barrier": 0.0012,
    "max_barrier": 0.02,
    "default_target_pct": 0.004,
    "confidence_sizing": True,
    "min_size_scale": 0.25,
    "entry_order_type": "MARKET",
    "activation_bounds": None,
    "manual_kill_switch": False,
    "initial_positions": [],
}

GLOBAL = {
    "mqtt_bridge.mqtt_host": "localhost",
    "mqtt_bridge.mqtt_port": 1883,
    "mqtt_bridge.mqtt_namespace": "hbot",
    "mqtt_bridge.mqtt_autostart": True,
    "mqtt_bridge.mqtt_commands": True,
    "mqtt_bridge.mqtt_events": True,
    "mqtt_bridge.mqtt_external_events": True,
    "mqtt_bridge.mqtt_ssl": False,
    "log_level": "INFO",
    "rate_oracle_source": "Binance",
    "global_token.global_token_name": "USDT",
}

DOCTOR_CHECKS = [
    {"check": "install", "status": "ok", "detail": "source checkout, conda env hummingbot"},
    {"check": "extensions", "status": "ok", "detail": "Cython extensions built and importable"},
    {"check": "keystore", "status": "ok", "detail": "keystore unlockable"},
    {"check": "clock", "status": "ok", "detail": "skew 0.12s vs pool.ntp.org"},
    {"check": "disk", "status": "ok", "detail": "18.4 GB free"},
    {"check": "bot", "status": "ok", "detail": "no stale bot.pid"},
    {"check": "loaded config", "status": "ok", "detail": f"{CONFIG_FILE} (controller) present"},
]

LOG_LINES = [
    " - INFO - AI scalper subscribed to MQTT topic: hbot/predictions/btc_usdt/ML_SIGNALS",
    " - INFO - Connecting MQTT Bridge...",
    " - INFO - MQTT Bridge connected to localhost:1883",
    " - INFO - Creating the clock...",
    " - INFO - Added 1 market(s) to the clock",
    " - INFO - Started strategy v2_with_controllers",
    " - INFO - signal:  1  confidence: 0.612  stale: False  age: 3.4s  dropped: 0",
    " - INFO - Created position executor 8f2a1c (BUY) amount=0.00153 BTC",
    " - INFO - Order filled: BUY 0.00153 BTC at 65431.20 (maker)",
    " - INFO - Take profit limit order placed at 65627.50",
    " - INFO - Executor 8f2a1c closed: TAKE_PROFIT net_pnl_quote=+0.28 net_pnl_pct=+0.30%",
    " - INFO - signal:  0  confidence: 0.481  stale: False  age: 2.1s  dropped: 0",
    " - INFO - Cooldown active: 21s remaining",
    " - INFO - signal: -1  confidence: 0.588  stale: False  age: 1.7s  dropped: 0",
    " - INFO - Created position executor c31d09 (SELL) amount=0.00149 BTC",
    " - WARNING - Dropped malformed ML signal (1 total): {'probabilities': [0.5, 0.5]}",
    " - INFO - Executor c31d09 closed: STOP_LOSS net_pnl_quote=-0.31 net_pnl_pct=-0.42%",
    " - INFO - signal:  1  confidence: 0.639  stale: False  age: 4.0s  dropped: 1",
]


class DemoBot:
    """Advances a fake bot on each read so the dashboard visibly moves."""

    def __init__(self, seed: int = 7):
        self._rng = random.Random(seed)
        self.started_at = time.time() - 3725.0
        self.price = 65432.10
        self.trades = 14
        self.gross = 3.82
        self.fees = 5.41
        self.net = -1.59
        self.errors = 1
        self._tick = 0
        self.running = True
        self.fields: Dict[str, object] = dict(FIELDS)

    def advance(self) -> None:
        self._tick += 1
        self.price *= 1 + self._rng.gauss(0.00002, 0.0006)
        if self._tick % 4 == 0:
            self.trades += 1
            gross_delta = self._rng.gauss(0.05, 0.42)
            fee_delta = abs(gross_delta) * 0.06 + 0.04
            self.gross += gross_delta
            self.fees += fee_delta
            self.net += gross_delta - fee_delta
        if self._tick % 37 == 0:
            self.errors += 1

    def signal(self) -> dict:
        phase = math.sin(self._tick / 9.0) + self._rng.gauss(0, 0.12)
        long_p = max(0.0, min(1.0, 0.5 + 0.32 * phase))
        short_p = max(0.0, min(1.0, 0.5 - 0.32 * phase))
        scale = max(long_p, short_p)
        if scale > 0.5:
            long_p, short_p = long_p / scale * 0.62, short_p / scale * 0.62
        neutral = max(0.0, 1.0 - long_p - short_p)
        if long_p > float(self.fields["long_threshold"]) and long_p >= short_p:
            state, confidence = 1, long_p
        elif short_p > float(self.fields["short_threshold"]) and short_p > long_p:
            state, confidence = -1, short_p
        else:
            state, confidence = 0, max(long_p, short_p)
        return {
            "signal": state,
            "confidence": round(confidence, 4),
            "probabilities": [round(short_p, 4), round(neutral, 4), round(long_p, 4)],
            "target_pct": round(0.0032 + 0.0012 * abs(phase), 5),
            "stale": False,
            "age_s": round(self._rng.uniform(0.4, 6.0), 1),
            "dropped": 1,
            "malformed": 1,
            "thresholds": {"long": float(self.fields["long_threshold"]),
                           "short": float(self.fields["short_threshold"]),
                           "timeout": float(self.fields["signal_timeout"])},
            "price": round(self.price, 2),
            "model": "baseline_heuristic",
            "source": "demo",
        }

    def status(self) -> dict:
        ai = self.signal()
        barriers = (f"target_pct: {ai['target_pct']}  sl: 0.0048  tp: 0.0032  "
                    f"time_limit: 600s  entry: MARKET")
        head = (f"signal: {ai['signal']:>2}  confidence: {ai['confidence']:.3f}  "
                f"stale: {ai['stale']}  age: {ai['age_s']}s  dropped: {ai['dropped']}")
        return {
            "running": self.running,
            "name": "conf_ai_scalper",
            "pid": 41237,
            "config": CONFIG_FILE,
            "type": "controller",
            "strategy": "v2_with_controllers",
            "uptime_s": round(time.time() - self.started_at, 1),
            "snapshot_age_s": 0.4,
            "errors": {"count": self.errors, "window": 600,
                       "messages": ["binance_paper_trade: order book refresh slower than 1s"]
                       if self.errors else []},
            "format_status": "\n".join([
                "  Controller: ai_scalper_btc_usdt_paper_v1",
                f"  Mid price: {self.price:.2f}",
                head, barriers,
                "  Active executors: 1  |  Closed: %d" % max(0, self.trades - 1),
            ]),
            "balances": {CONNECTOR: {"BTC": 0.0213, "USDT": 1391.44 + self.net}},
        }

    def history(self) -> List[dict]:
        return [{
            "market": CONNECTOR, "pair": PAIR, "trades": self.trades,
            "buys": self.trades // 2, "sells": self.trades - self.trades // 2,
            "base_vol": round(self.trades * 0.00151, 5),
            "quote_vol": round(self.trades * 0.00151 * self.price, 2),
            "trade_pnl": round(self.gross, 4), "fees": round(self.fees, 4),
            "total_pnl": round(self.net, 4),
            "return%": round(self.net / 1400.0 * 100, 4),
        }]

    def config(self) -> dict:
        return {
            "global": dict(GLOBAL),
            "strategy": {"file": CONFIG_FILE, "type": "controller",
                         "state": "running" if self.running else "loaded",
                         "running": self.running,
                         "fields": dict(self.fields), "live_fields": list(LIVE_FIELDS)},
        }

    def logs(self, lines: int = 200) -> dict:
        n = min(lines, len(LOG_LINES))
        base = int(time.time() - self.started_at)
        stamped = [f"2026-10-07 {13 + (base + i * 7) // 3600 % 9:02d}:"
                   f"{(base + i * 7) // 60 % 60:02d}:{(base + i * 7) % 60:02d},104 - 41237 - "
                   f"hummingbot.strategy_v2{line}"
                   for i, line in enumerate(LOG_LINES[-n:])]
        return {"file": "logs/logs_conf_ai_scalper.log", "lines": stamped}

    def doctor(self) -> dict:
        return {"healthy": True, "checks": [dict(c) for c in DOCTOR_CHECKS]}


_DEMO: Optional[DemoBot] = None


def demo_bot() -> DemoBot:
    global _DEMO
    if _DEMO is None:
        _DEMO = DemoBot()
    return _DEMO
