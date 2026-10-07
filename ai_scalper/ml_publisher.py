#!/usr/bin/env python3
"""ML brain for the Hummingbot AI scalper: publishes predictions to MQTT.

Runs OUTSIDE the Hummingbot process (dev box, a second VPS, or the same VPS). The controller
`controllers/directional_trading/ai_scalper.py` subscribes to
`<topic_prefix>/<pair>/ML_SIGNALS` and turns each message into a PositionExecutor.

Secrets come from the environment (MQTT_USERNAME / MQTT_PASSWORD) — never from a config file.

    python ml_publisher.py --pair BTC-USDT --interval 1m --every 15
    python ml_publisher.py --pair BTC-USDT --dry-run --once
"""
import argparse
import json
import logging
import os
import signal
import sys
import time
from typing import Optional

import pandas as pd
import requests

from baseline_model import build_model

KLINE_ENDPOINTS = {
    "binance": "https://api.binance.com/api/v3/klines",
    "binance_testnet": "https://testnet.binance.vision/api/v3/klines",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ml_publisher")


class Publisher:
    """MQTT publisher with automatic reconnect; `--dry-run` prints instead of connecting."""

    def __init__(self, host: str, port: int, username: str = "", password: str = "",
                 ssl: bool = False, dry_run: bool = False, client_id: str = "ai_scalper_ml"):
        self.dry_run = dry_run
        self.client = None
        if dry_run:
            return
        import paho.mqtt.client as mqtt
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        if username:
            self.client.username_pw_set(username, password)
        if ssl:
            self.client.tls_set()
        self.client.connect_async(host, port, keepalive=30)
        self.client.loop_start()

    def publish(self, topic: str, payload: dict) -> None:
        body = json.dumps(payload)
        if self.dry_run:
            log.info("[dry-run] %s %s", topic, body)
            return
        self.client.publish(topic, body, qos=0)

    def close(self) -> None:
        if self.client is not None:
            self.client.loop_stop()
            self.client.disconnect()


def fetch_klines(exchange: str, pair: str, interval: str, limit: int,
                 timeout: float = 10.0) -> Optional[pd.DataFrame]:
    endpoint = KLINE_ENDPOINTS.get(exchange)
    if endpoint is None:
        raise ValueError(f"unsupported exchange '{exchange}'; known: {sorted(KLINE_ENDPOINTS)}")
    symbol = pair.replace("-", "").upper()
    resp = requests.get(endpoint, params={"symbol": symbol, "interval": interval, "limit": limit},
                        timeout=timeout)
    resp.raise_for_status()
    raw = resp.json()
    if not raw:
        return None
    df = pd.DataFrame(raw, columns=["open_time", "open", "high", "low", "close", "volume",
                                    "close_time", "quote_vol", "trades", "taker_base",
                                    "taker_quote", "ignore"])
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df[["open_time", "open", "high", "low", "close", "volume"]].dropna()


def signal_topic(prefix: str, pair: str) -> str:
    return f"{prefix}/{pair.replace('-', '_').lower()}/ML_SIGNALS"


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Publish ML scalping signals to MQTT for Hummingbot.")
    p.add_argument("--pair", default="BTC-USDT")
    p.add_argument("--exchange", default="binance", choices=sorted(KLINE_ENDPOINTS))
    p.add_argument("--interval", default="1m", help="candle interval, e.g. 1m / 3m / 5m")
    p.add_argument("--limit", type=int, default=300, help="candles fetched per cycle")
    p.add_argument("--every", type=float, default=15.0, help="seconds between predictions")
    p.add_argument("--topic-prefix", default="hbot/predictions")
    p.add_argument("--host", default=os.environ.get("MQTT_HOST", "localhost"))
    p.add_argument("--port", type=int, default=int(os.environ.get("MQTT_PORT", "1883")))
    p.add_argument("--ssl", action="store_true", default=os.environ.get("MQTT_SSL", "") == "true")
    p.add_argument("--model", default=os.environ.get("MODEL_PATH"),
                   help="joblib model with predict_proba; omit for the baseline heuristic")
    p.add_argument("--dry-run", action="store_true", help="log predictions without MQTT")
    p.add_argument("--once", action="store_true", help="publish a single prediction and exit")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)
    if args.verbose:
        log.setLevel(logging.DEBUG)
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    model = build_model(args.model)
    publisher = Publisher(
        host=args.host, port=args.port, ssl=args.ssl, dry_run=args.dry_run,
        username=os.environ.get("MQTT_USERNAME", ""), password=os.environ.get("MQTT_PASSWORD", ""),
    )
    topic = signal_topic(args.topic_prefix, args.pair)
    log.info("publishing to %s every %.0fs (pair=%s interval=%s)", topic, args.every, args.pair, args.interval)

    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))

    failures = 0
    try:
        while not stop["flag"]:
            cycle_start = time.time()
            try:
                candles = fetch_klines(args.exchange, args.pair, args.interval, args.limit)
                if candles is None or candles.empty:
                    raise ValueError("empty candle response")
                prediction = model.predict(candles)
                prediction.update({
                    "pair": args.pair,
                    "interval": args.interval,
                    "candle_close": candles["close"].iloc[-1],
                    "ts": time.time(),
                    "model": os.path.basename(args.model) if args.model else "baseline_heuristic",
                })
                publisher.publish(topic, prediction)
                log.info("%s -> probabilities=%s target_pct=%s",
                         args.pair, prediction["probabilities"], prediction["target_pct"])
                failures = 0
            except Exception as e:
                failures += 1
                log.error("cycle failed (%d in a row): %s", failures, e)
                if failures >= 5:
                    log.error("too many consecutive failures, exiting")
                    return 1
            if args.once:
                break
            remaining = args.every - (time.time() - cycle_start)
            deadline = time.time() + max(0.0, remaining)
            while not stop["flag"] and time.time() < deadline:
                time.sleep(min(0.5, max(0.0, deadline - time.time())))
    finally:
        publisher.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
