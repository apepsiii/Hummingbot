import time
from decimal import Decimal
from typing import List, Optional

from pydantic import Field, field_validator

from hummingbot.core.data_type.common import OrderType, PriceType, TradeType
from hummingbot.remote_iface.mqtt import ExternalTopicFactory
from hummingbot.strategy_v2.controllers.directional_trading_controller_base import (
    DirectionalTradingControllerBase,
    DirectionalTradingControllerConfigBase,
)
from hummingbot.strategy_v2.executors.position_executor.data_types import PositionExecutorConfig, TripleBarrierConfig
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, ExecutorAction, StopExecutorAction
from hummingbot.strategy_v2.utils.common import parse_enum_value


class AIScalperControllerConfig(DirectionalTradingControllerConfigBase):
    """
    Directional controller whose signal source is an external AI/ML process publishing over MQTT.

    The controller owns no prediction logic: it only translates a probability triple into a
    PositionExecutor with tight, volatility-aware triple barriers, and it refuses to trade on a
    stale signal. Every tuning field is live-updatable so an external supervisor (LLM or script)
    can retune the bot with `hbot config <key> <value>` while it runs.
    """
    controller_name: str = "ai_scalper"

    topic: str = "hbot/predictions"
    long_threshold: float = Field(default=0.60, ge=0.0, le=1.0, json_schema_extra={"is_updatable": True})
    short_threshold: float = Field(default=0.60, ge=0.0, le=1.0, json_schema_extra={"is_updatable": True})

    signal_timeout: int = Field(default=30, gt=0, json_schema_extra={"is_updatable": True})
    close_on_stale_signal: bool = Field(default=True, json_schema_extra={"is_updatable": True})

    sl_multiplier: float = Field(default=1.5, gt=0, json_schema_extra={"is_updatable": True})
    tp_multiplier: float = Field(default=1.0, gt=0, json_schema_extra={"is_updatable": True})
    min_barrier: Decimal = Field(default=Decimal("0.0012"), gt=0, json_schema_extra={"is_updatable": True})
    max_barrier: Decimal = Field(default=Decimal("0.02"), gt=0, json_schema_extra={"is_updatable": True})
    default_target_pct: Decimal = Field(default=Decimal("0.004"), gt=0)

    confidence_sizing: bool = Field(default=True, json_schema_extra={"is_updatable": True})
    min_size_scale: float = Field(default=0.25, ge=0.0, le=1.0, json_schema_extra={"is_updatable": True})

    entry_order_type: OrderType = Field(default=OrderType.MARKET, json_schema_extra={"is_updatable": True})
    activation_bounds: Optional[List[Decimal]] = Field(default=None, json_schema_extra={"is_updatable": True})

    @field_validator("entry_order_type", mode="before")
    @classmethod
    def validate_entry_order_type(cls, v) -> OrderType:
        if v is None:
            return OrderType.MARKET
        if isinstance(v, str):
            v = v.replace("OrderType.", "")
        return parse_enum_value(OrderType, v, "entry_order_type")

    @field_validator("min_barrier", "max_barrier", "default_target_pct", mode="before")
    @classmethod
    def validate_decimals(cls, v):
        if v is None or v == "":
            return None
        return Decimal(str(v))

    @field_validator("activation_bounds", mode="before")
    @classmethod
    def parse_activation_bounds(cls, v):
        if v is None or v == "":
            return None
        bounds = [Decimal(x) for x in v.split(",")] if isinstance(v, str) else list(v)
        if len(bounds) != 2:
            raise ValueError("activation_bounds needs exactly 2 values, e.g. '0.002,0.002'")
        return bounds


class AIScalperController(DirectionalTradingControllerBase):
    def __init__(self, config: AIScalperControllerConfig, *args, **kwargs):
        super().__init__(config, *args, **kwargs)
        self.config = config
        self._signal: int = 0
        self._confidence: float = 0.0
        self._target_pct: Decimal = config.default_target_pct
        self._last_signal_ts: Optional[float] = None
        self._malformed_signals: int = 0
        self.processed_data = {"signal": 0, "confidence": 0.0, "target_pct": config.default_target_pct,
                               "stale": True, "last_signal_age": None}
        self._ml_signal_listener = None
        self._init_ml_signal_listener()

    def _init_ml_signal_listener(self):
        try:
            normalized_pair = self.config.trading_pair.replace("-", "_").lower()
            topic = f"{self.config.topic}/{normalized_pair}/ML_SIGNALS"
            self._ml_signal_listener = ExternalTopicFactory.create_async(
                topic=topic,
                callback=self._handle_ml_signal,
                use_bot_prefix=False,
            )
            self.logger().info(f"AI scalper subscribed to MQTT topic: {topic}")
        except Exception as e:
            self._ml_signal_listener = None
            self.logger().error(
                f"Failed to subscribe to MQTT signals: {e}. "
                "Set mqtt_bridge.mqtt_autostart=true in conf_client.yml and restart the bot."
            )

    def _handle_ml_signal(self, signal: dict, topic: str):
        """Consume one prediction: {"probabilities": [short, neutral, long], "target_pct": 0.004}.

        Runs on a bridge thread, so it only writes plain scalars and never touches the market data
        provider. Anything malformed is counted and dropped instead of trading on garbage.
        """
        try:
            probabilities = signal["probabilities"]
            short, neutral, long = float(probabilities[0]), float(probabilities[1]), float(probabilities[2])
        except (KeyError, IndexError, TypeError, ValueError):
            self._malformed_signals += 1
            self.logger().warning(f"Dropped malformed ML signal ({self._malformed_signals} total): {signal}")
            return
        if min(short, neutral, long) < 0.0 or max(short, neutral, long) > 1.0:
            self._malformed_signals += 1
            self.logger().warning(f"Dropped out-of-range ML signal: probabilities={probabilities}")
            return

        if long > self.config.long_threshold and long >= short:
            self._signal, self._confidence = 1, long
        elif short > self.config.short_threshold and short > long:
            self._signal, self._confidence = -1, short
        else:
            self._signal, self._confidence = 0, max(long, short)

        raw_target = signal.get("target_pct")
        try:
            self._target_pct = self._clamp_barrier(Decimal(str(raw_target)))
        except (TypeError, ValueError, ArithmeticError):
            self._target_pct = self.config.default_target_pct
        self._last_signal_ts = time.time()

    def _is_stale(self) -> bool:
        if self._last_signal_ts is None:
            return True
        return time.time() - self._last_signal_ts > self.config.signal_timeout

    async def update_processed_data(self):
        stale = self._is_stale()
        if stale:
            self._signal = 0
        age = None if self._last_signal_ts is None else time.time() - self._last_signal_ts
        self.processed_data = {
            "signal": self._signal,
            "confidence": self._confidence,
            "target_pct": self._target_pct,
            "stale": stale,
            "last_signal_age": age,
        }

    def create_actions_proposal(self) -> List[ExecutorAction]:
        signal = self.processed_data["signal"]
        if signal == 0 or not self.can_create_executor(signal):
            return []
        price = self.market_data_provider.get_price_by_type(
            self.config.connector_name, self.config.trading_pair, PriceType.MidPrice)
        amount = self.config.total_amount_quote / price / Decimal(self.config.max_executors_per_side)
        amount = amount * Decimal(str(self._size_scale(signal)))
        trade_type = TradeType.BUY if signal > 0 else TradeType.SELL
        return [CreateExecutorAction(
            controller_id=self.config.id,
            executor_config=self.get_executor_config(trade_type, price, amount))]

    def stop_actions_proposal(self) -> List[ExecutorAction]:
        """Flatten the book when the AI stops talking, so a dead publisher can't strand a position."""
        if not (self.processed_data.get("stale") and self.config.close_on_stale_signal):
            return []
        active = self.filter_executors(executors=self.executors_info, filter_func=lambda x: x.is_active)
        return [StopExecutorAction(controller_id=self.config.id, executor_id=executor.id)
                for executor in active]

    def _size_scale(self, signal: int) -> float:
        if not self.config.confidence_sizing:
            return 1.0
        threshold = self.config.long_threshold if signal > 0 else self.config.short_threshold
        edge = (self._confidence - threshold) / max(1e-9, 1.0 - threshold)
        return min(1.0, max(self.config.min_size_scale, edge))

    def _clamp_barrier(self, value: Decimal) -> Decimal:
        return min(self.config.max_barrier, max(self.config.min_barrier, value))

    def get_executor_config(self, trade_type: TradeType, price: Decimal, amount: Decimal):
        return PositionExecutorConfig(
            timestamp=self.market_data_provider.time(),
            connector_name=self.config.connector_name,
            trading_pair=self.config.trading_pair,
            side=trade_type,
            entry_price=price,
            amount=amount,
            triple_barrier_config=self._triple_barrier_config(),
            leverage=self.config.leverage,
            activation_bounds=self.config.activation_bounds,
        )

    def _triple_barrier_config(self) -> TripleBarrierConfig:
        target = self._target_pct
        stop_loss = self._clamp_barrier(target * Decimal(str(self.config.sl_multiplier)))
        take_profit = self._clamp_barrier(target * Decimal(str(self.config.tp_multiplier)))
        return TripleBarrierConfig(
            stop_loss=stop_loss,
            take_profit=take_profit,
            time_limit=self.config.time_limit,
            trailing_stop=self.config.trailing_stop,
            open_order_type=self.config.entry_order_type,
            take_profit_order_type=self.config.take_profit_order_type,
            stop_loss_order_type=OrderType.MARKET,
            time_limit_order_type=OrderType.MARKET,
        )

    def to_format_status(self) -> List[str]:
        data = self.processed_data
        age = data.get("last_signal_age")
        age_str = "never" if age is None else f"{age:.1f}s"
        barriers = self._triple_barrier_config()
        return [
            f"signal: {data.get('signal'):>2}  confidence: {data.get('confidence', 0.0):.3f}  "
            f"stale: {data.get('stale')}  age: {age_str}  dropped: {self._malformed_signals}",
            f"target_pct: {data.get('target_pct')}  sl: {barriers.stop_loss}  tp: {barriers.take_profit}  "
            f"time_limit: {barriers.time_limit}s  entry: {barriers.open_order_type.name}",
        ]
