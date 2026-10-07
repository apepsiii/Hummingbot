"""Live AI-signal feed for the web UI: subscribes to the MQTT topics the publisher writes to.

Purely observational — it never publishes and never touches the bot. If aiomqtt is missing or the
broker is unreachable, it stays disabled and reports why, so the dashboard still works.
"""
import asyncio
import json
import logging
import time
from collections import deque
from typing import Dict, List, Optional

log = logging.getLogger("webui.signals")

DEFAULT_TOPIC_FILTER = "hbot/predictions/#"
RECONNECT_MIN = 1.0
RECONNECT_MAX = 30.0
SERIES_LEN = 240
LATEST_LEN = 32


def pair_from_topic(topic: str) -> Optional[str]:
    """`hbot/predictions/btc_usdt/ML_SIGNALS` -> `BTC-USDT` (the controller's pair format)."""
    parts = topic.split("/")
    if len(parts) < 3:
        return None
    return parts[-2].replace("_", "-").upper()


class SignalFeed:
    def __init__(self, host: str = "localhost", port: int = 1883, username: str = "",
                 password: str = "", ssl: bool = False,
                 topic_filter: str = DEFAULT_TOPIC_FILTER, enabled: bool = True):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.ssl = ssl
        self.topic_filter = topic_filter
        self.enabled = enabled
        self.connected = False
        self.message_count = 0
        self.malformed = 0
        self.last_error: Optional[str] = None
        self.last_message_ts: Optional[float] = None
        self.latest: Dict[str, dict] = {}
        self.series: Dict[str, deque] = {}
        self.recent: deque = deque(maxlen=LATEST_LEN)
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if not self.enabled:
            self.last_error = "disabled (set --mqtt-host to enable)"
            return
        try:
            import aiomqtt  # noqa: F401
        except ImportError:
            self.enabled = False
            self.last_error = "aiomqtt not installed"
            log.warning("MQTT feed disabled: aiomqtt not installed")
            return
        self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log.debug("signal feed task ended: %s", e)
        self.connected = False

    async def _run(self) -> None:
        import aiomqtt
        backoff = RECONNECT_MIN
        while not self._stop.is_set():
            try:
                tls_params = aiomqtt.TLSParameters() if self.ssl else None
                async with aiomqtt.Client(hostname=self.host, port=self.port,
                                          username=self.username or None,
                                          password=self.password or None,
                                          identifier="hbot-webui",
                                          tls_params=tls_params, keepalive=60) as client:
                    await client.subscribe(self.topic_filter)
                    self.connected = True
                    self.last_error = None
                    backoff = RECONNECT_MIN
                    log.info("MQTT feed connected to %s:%s (%s)", self.host, self.port,
                             self.topic_filter)
                    async for message in client.messages:
                        if self._stop.is_set():
                            break
                        self._consume(str(message.topic), message.payload)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"[:200]
            finally:
                self.connected = False
            if self._stop.is_set():
                break
            log.warning("MQTT feed disconnected (%s); retrying in %.0fs",
                        self.last_error or "unknown", backoff)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, RECONNECT_MAX)

    def _consume(self, topic: str, payload) -> None:
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            self.malformed += 1
            return
        if not isinstance(data, dict):
            self.malformed += 1
            return
        pair = pair_from_topic(topic)
        if pair is None:
            return
        try:
            probs = [float(p) for p in data["probabilities"][:3]]
            if len(probs) != 3:
                raise ValueError("need 3 probabilities")
        except (KeyError, IndexError, TypeError, ValueError):
            self.malformed += 1
            return

        now = time.time()
        record = {
            "pair": pair,
            "topic": topic,
            "probabilities": [round(p, 4) for p in probs],
            "target_pct": data.get("target_pct"),
            "price": data.get("candle_close"),
            "model": data.get("model"),
            "ts": float(data.get("ts") or now),
            "received_ts": now,
        }
        self.latest[pair] = record
        self.series.setdefault(pair, deque(maxlen=SERIES_LEN)).append(
            {"ts": record["ts"], "short": probs[0], "neutral": probs[1], "long": probs[2],
             "target_pct": record["target_pct"]})
        self.recent.append(record)
        self.message_count += 1
        self.last_message_ts = now

    def enrich(self, thresholds: Dict[str, float], timeout_s: float = 30.0) -> Dict[str, dict]:
        """Apply the controller's live thresholds so the UI shows the state the bot will act on."""
        out: Dict[str, dict] = {}
        long_t = float(thresholds.get("long_threshold", 0.55))
        short_t = float(thresholds.get("short_threshold", 0.55))
        now = time.time()
        for pair, record in self.latest.items():
            short, neutral, long = record["probabilities"]
            if long > long_t and long >= short:
                signal, confidence = 1, long
            elif short > short_t and short > long:
                signal, confidence = -1, short
            else:
                signal, confidence = 0, max(long, short)
            age = now - record["received_ts"]
            enriched = dict(record)
            enriched.update({
                "signal": signal,
                "confidence": round(confidence, 4),
                "thresholds": {"long": long_t, "short": short_t, "timeout": timeout_s},
                "stale": age > timeout_s,
                "age_s": round(age, 1),
                "malformed": self.malformed,
            })
            out[pair] = enriched
        return out

    def snapshot(self) -> dict:
        return {
            "enabled": self.enabled,
            "connected": self.connected,
            "broker": f"{self.host}:{self.port}{' (tls)' if self.ssl else ''}",
            "topic_filter": self.topic_filter,
            "message_count": self.message_count,
            "malformed": self.malformed,
            "last_error": self.last_error,
            "last_message_age_s": (round(time.time() - self.last_message_ts, 1)
                                    if self.last_message_ts else None),
            "pairs": sorted(self.latest),
        }

    def series_for(self, pair: str, limit: int = SERIES_LEN) -> List[dict]:
        return list(self.series.get(pair, ()))[: max(1, min(limit, SERIES_LEN))]
