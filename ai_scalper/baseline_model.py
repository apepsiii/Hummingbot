"""Prediction models for the AI scalper.

Contract every model must satisfy:

    predict(candles: pd.DataFrame) -> dict
        {"probabilities": [short, neutral, long], "target_pct": float}

`probabilities` are floats in [0, 1]; `target_pct` is the expected move the model is betting on,
used by the controller to size the triple barrier.
"""
from typing import Optional

import numpy as np
import pandas as pd

FEATURES = ["ret_1_z", "ret_5_z", "ret_15_z", "rsi_c", "ema_spread_z", "vol_z", "imbalance", "bb_pos"]
Z_CLIP = 3.0


def compute_features(candles: pd.DataFrame) -> pd.DataFrame:
    """Turn OHLCV candles into the feature frame every model here consumes.

    Every feature is normalised to roughly [-1, 1] (returns are expressed in ATR units) so that
    weights are comparable across pairs and timeframes instead of being swamped by whichever
    feature happens to carry the largest raw magnitude.
    """
    df = candles.copy()
    close, high, low, volume = df["close"], df["high"], df["low"], df["volume"]

    prev_close = close.shift(1)
    true_range = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    df["atr_pct"] = (true_range.rolling(14).mean() / close).bfill()
    vol_unit = df["atr_pct"].replace(0.0, np.nan)

    df["ret_1_z"] = (close.pct_change() / vol_unit).clip(-Z_CLIP, Z_CLIP)
    df["ret_5_z"] = (close.pct_change(5) / vol_unit / np.sqrt(5)).clip(-Z_CLIP, Z_CLIP)
    df["ret_15_z"] = (close.pct_change(15) / vol_unit / np.sqrt(15)).clip(-Z_CLIP, Z_CLIP)

    delta = close.diff()
    gain = delta.clip(lower=0.0).rolling(14).mean()
    loss = (-delta.clip(upper=0.0)).rolling(14).mean()
    rs = gain / loss.replace(0.0, np.nan)
    df["rsi_c"] = ((100 - 100 / (1 + rs)).fillna(50.0) / 50.0) - 1.0

    df["ema_spread_z"] = (((close.ewm(span=9).mean() / close.ewm(span=21).mean()) - 1.0)
                          / vol_unit).clip(-Z_CLIP, Z_CLIP)

    vol_mean, vol_std = volume.rolling(50).mean(), volume.rolling(50).std()
    df["vol_z"] = ((volume - vol_mean) / vol_std.replace(0.0, np.nan)).clip(-2, 2) / 2.0

    span = (high - low).replace(0.0, np.nan)
    df["imbalance"] = (((close - low) - (high - close)) / span).fillna(0.0)

    mid = close.rolling(20).mean()
    std = close.rolling(20).std().replace(0.0, np.nan)
    df["bb_pos"] = ((close - mid) / (2 * std)).clip(-1, 1)

    return df.dropna(subset=FEATURES + ["atr_pct"])


def volatility_target(features: pd.DataFrame) -> float:
    """Expected move to bet on, derived from realized volatility and clamped to scalping range."""
    atr = float(features["atr_pct"].iloc[-1])
    return float(round(min(0.01, max(0.001, atr * 1.5)), 6))


class BaselineHeuristicModel:
    """Transparent stand-in for a trained model. Not an edge — replace it before risking money.

    Weighted sum of volatility-normalised momentum, mean-reversion and order-flow features,
    squashed with tanh. A quiet market scores near zero and stays neutral, so the bot does not
    trade; only a clear trend pushes one side above the controller's threshold.
    """

    weights = {
        "ret_1_z": 0.35,
        "ret_5_z": 0.35,
        "ret_15_z": 0.30,
        "ema_spread_z": 0.40,
        "imbalance": 0.30,
        "vol_z": 0.15,
        "rsi_c": -0.30,
        "bb_pos": -0.25,
    }
    score_scale = 2.0

    def __init__(self, neutral_floor: float = 0.34, min_confidence: float = 0.05):
        self.neutral_floor = neutral_floor
        self.min_confidence = min_confidence

    def score(self, features: pd.DataFrame) -> float:
        row = features.iloc[-1]
        return float(sum(self.weights[name] * float(row[name]) for name in FEATURES))

    def predict(self, candles: pd.DataFrame) -> dict:
        features = compute_features(candles)
        if features.empty:
            return {"probabilities": [0.0, 1.0, 0.0], "target_pct": 0.002}

        score = float(np.tanh(self.score(features) / self.score_scale))
        target = volatility_target(features)
        if abs(score) < self.min_confidence:
            return {"probabilities": [0.0, 1.0, 0.0], "target_pct": target}

        directional = (1.0 - self.neutral_floor) * abs(score)
        long_p = directional if score > 0 else 0.0
        short_p = directional if score < 0 else 0.0
        neutral = 1.0 - long_p - short_p
        return {"probabilities": [round(float(short_p), 6), round(float(neutral), 6),
                                  round(float(long_p), 6)],
                "target_pct": target}


class TrainedModel:
    """Adapter for a scikit-learn-compatible classifier exposing `predict_proba`.

    Classes must be ordered [short, neutral, long] — pass `class_order` if your model differs.
    """

    def __init__(self, model_path: str, class_order: Optional[list] = None, fallback=None):
        import joblib
        self.model = joblib.load(model_path)
        self.class_order = class_order or [0, 1, 2]
        self.fallback = fallback or BaselineHeuristicModel()

    def predict(self, candles: pd.DataFrame) -> dict:
        features = compute_features(candles)
        if features.empty:
            return self.fallback.predict(candles)
        raw = np.asarray(self.model.predict_proba(features[FEATURES].tail(1)))[0]
        probs = [float(raw[i]) for i in self.class_order]
        total = sum(probs) or 1.0
        probs = [p / total for p in probs]
        return {"probabilities": [round(p, 6) for p in probs],
                "target_pct": volatility_target(features)}


def build_model(model_path: Optional[str] = None):
    return TrainedModel(model_path) if model_path else BaselineHeuristicModel()
