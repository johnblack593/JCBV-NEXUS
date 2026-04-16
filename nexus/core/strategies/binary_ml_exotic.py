"""
NEXUS v4.0 — Binary ML Exotic Strategy (IQ Option Exclusive)
==============================================================
Estrategia de Machine Learning "on-the-fly" para Binary Options.

Arquitectura:
    - Random Forest classifier entrenado en cada tick (500 filas).
    - Features: Log-Returns, ATR Volatility, RSI Momentum.
    - Target: 1 si next close > current close, 0 otherwise.
    - Train slice: [0:499], Predict: [500] (última vela).
    - El modelo se INSTANCIA, ENTRENA y DESTRUYE en cada ciclo.

Diseñada para IQ Option Binary/Turbo (1-5 min expiration).
NO genera SL/TP (binarias tienen expiration fija).
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import numpy as np  # type: ignore
import pandas as pd  # type: ignore

from .base import BaseStrategy

logger = logging.getLogger("nexus.strategies.binary_ml_exotic")

# ── Lazy import scikit-learn ───────────────────────────────────────
try:
    from sklearn.ensemble import RandomForestClassifier  # type: ignore
    _HAS_SKLEARN = True
except ImportError:
    _HAS_SKLEARN = False
    logger.warning("scikit-learn no disponible. BinaryMLExoticStrategy opearará en fallback.")


class BinaryMLExoticStrategy(BaseStrategy):
    """
    ML On-the-fly para Binary Options HFT.

    Principio: Cada tick es un micro-experimento.
    El modelo vive y muere en `analyze()`. No hay state leakage.

    Performance: ~15ms para 500 filas × 3 features × 100 trees.
    Esto es aceptable para el tick interval de 60s en IQ Mode.
    """

    MIN_ROWS: int = 100     # Mínimo de filas para entrenar
    TRAIN_SIZE: int = 499   # Filas para entrenamiento [0:499]
    N_ESTIMATORS: int = 100 # Árboles en el Random Forest
    RSI_PERIOD: int = 14
    ATR_PERIOD: int = 14

    async def analyze(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Entrena un Random Forest on-the-fly y genera señal para la última vela.

        Flow:
            1. Genera features: Returns, ATR, RSI
            2. Target: 1 si close[t+1] > close[t], else 0
            3. Train en [0:TRAIN_SIZE], predict en [TRAIN_SIZE]
            4. Retorna signal + la probabilidad como confidence

        Returns:
            {"signal": "BUY"|"SELL"|"HOLD", "confidence": float, "reason": str, ...}
        """
        if df is None or len(df) < self.MIN_ROWS:
            return self._hold("Datos insuficientes para ML on-the-fly")

        if not _HAS_SKLEARN:
            return self._fallback_heuristic(df)

        try:
            # ── Feature Engineering ──────────────────────────────────
            features_df = self._build_features(df)

            if features_df is None or len(features_df) < self.MIN_ROWS:
                return self._hold("Features insuficientes post-cálculo")

            # ── Target: 1 si close[t+1] > close[t] ──────────────────
            features_df["target"] = (
                features_df["close"].shift(-1) > features_df["close"]
            ).astype(int)

            # Drop rows with NaN (from shift and rolling calcs)
            features_df = features_df.dropna()

            if len(features_df) < self.MIN_ROWS:
                return self._hold("Datos post-dropna insuficientes")

            # ── Train/Predict Split ──────────────────────────────────
            feature_cols = ["log_returns", "atr", "rsi"]
            # Leakage-safe split: última fila = predicción, penúltima = buffer
            train_end = min(self.TRAIN_SIZE, len(features_df) - 2)

            X_train = features_df[feature_cols].iloc[:train_end].values
            y_train = features_df["target"].iloc[:train_end].values
            X_pred = features_df[feature_cols].iloc[-1:].values

            if len(X_train) < 50 or len(X_pred) == 0:
                return self._hold("Split insuficiente para ML")

            # ── Entrenamiento On-the-fly ─────────────────────────────
            clf = RandomForestClassifier(
                n_estimators=self.N_ESTIMATORS,
                max_depth=5,
                min_samples_split=10,
                random_state=None,  # Aleatoriedad real por tick — ensemble válido
                n_jobs=-1,
            )
            clf.fit(X_train, y_train)

            # ── Predicción ───────────────────────────────────────────
            proba = clf.predict_proba(X_pred)[0]  # [P(0), P(1)]

            # Clase con mayor probabilidad
            pred_class = int(np.argmax(proba))
            confidence = float(np.max(proba))

            # Feature importances para diagnóstico
            importances = dict(zip(feature_cols, clf.feature_importances_.tolist()))

            # ── Señal ────────────────────────────────────────────────
            if pred_class == 1 and confidence > 0.55:
                signal = "BUY"
                reason = (
                    f"RF predice UP (prob={confidence:.3f}) | "
                    f"Importances: {self._format_importances(importances)}"
                )
            elif pred_class == 0 and confidence > 0.55:
                signal = "SELL"
                reason = (
                    f"RF predice DOWN (prob={confidence:.3f}) | "
                    f"Importances: {self._format_importances(importances)}"
                )
            else:
                signal = "HOLD"
                reason = f"RF indeciso (prob={confidence:.3f}, class={pred_class})"

            # Extraer ATR real para el ConsensusEngine
            last_atr = float(features_df["atr"].iloc[-1]) if "atr" in features_df else 0.001

            # El clf se destruye aquí al salir del scope (GC)
            return {
                "signal": signal,
                "confidence": round(confidence, 4),
                "atr": last_atr,
                "reason": reason,
                "indicators": {
                    "rf_prediction": pred_class,
                    "rf_probability": round(confidence, 4),
                    "feature_importances": importances,
                    "train_samples": len(X_train),
                    # pred_entropy: 0.0 = certeza total, 1.0 = máxima incertidumbre (2 clases)
                    "pred_entropy": round(float(-np.sum(proba * np.log2(np.clip(proba, 1e-10, 1.0)))), 4),
                },
            }

        except Exception as exc:
            logger.error("BinaryMLExotic analyze error: %s", exc, exc_info=True)
            return self._hold(f"Error en ML: {exc}")

    # ══════════════════════════════════════════════════════════════════
    #  Feature Engineering
    # ══════════════════════════════════════════════════════════════════

    def _build_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Genera features para el Random Forest.

        Features:
            - log_returns: Log(close[t] / close[t-1])
            - atr: Average True Range (14 periodos) — volatilidad
            - rsi: Relative Strength Index (14 periodos) — momentum
        """
        out = df[["open", "high", "low", "close", "volume"]].copy()

        # Log Returns
        out["log_returns"] = np.log(out["close"] / out["close"].shift(1))

        # ATR (True Range → rolling mean)
        high_low = out["high"] - out["low"]
        high_prev_close = (out["high"] - out["close"].shift(1)).abs()
        low_prev_close = (out["low"] - out["close"].shift(1)).abs()
        true_range = pd.concat([high_low, high_prev_close, low_prev_close], axis=1).max(axis=1)
        out["atr"] = true_range.rolling(window=self.ATR_PERIOD).mean()

        # RSI
        delta = out["close"].diff()
        # Wilder's EWM smoothing — RSI estándar (alpha = 1/period)
        gain = delta.where(delta > 0, 0.0).ewm(
            alpha=1.0 / self.RSI_PERIOD, adjust=False
        ).mean()
        loss = (-delta.where(delta < 0, 0.0)).ewm(
            alpha=1.0 / self.RSI_PERIOD, adjust=False
        ).mean()
        rs = gain / loss.replace(0, np.finfo(float).eps)
        out["rsi"] = 100.0 - (100.0 / (1.0 + rs))

        return out

    # ══════════════════════════════════════════════════════════════════
    #  Helpers
    # ══════════════════════════════════════════════════════════════════

    def _hold(self, reason: str) -> Dict[str, Any]:
        """Retorna señal HOLD estandarizada."""
        return {
            "signal": "HOLD",
            "confidence": 0.0,
            "reason": reason,
            "indicators": {},
        }

    def _fallback_heuristic(self, df: pd.DataFrame) -> Dict[str, Any]:
        """Fallback cuando scikit-learn no está disponible."""
        closes = df["close"].values
        if len(closes) < 20:
            return self._hold("Fallback: datos insuficientes")

        # Simple momentum: últimos 5 retornos
        returns = np.diff(closes[-6:]) / closes[-6:-1]
        avg_return = float(np.mean(returns))

        if avg_return > 0.001:
            return self._hold(
                f"Fallback heurístico sin sklearn (momentum+={avg_return:.5f}) — HOLD conservador"
            )
        elif avg_return < -0.001:
            return self._hold(
                f"Fallback heurístico sin sklearn (momentum-={avg_return:.5f}) — HOLD conservador"
            )
        return self._hold("Fallback: sin momentum claro")

    @staticmethod
    def _format_importances(imp: Dict[str, float]) -> str:
        """Formatea importances para log legible."""
        return " | ".join(f"{k}={v:.2f}" for k, v in sorted(imp.items(), key=lambda x: -x[1]))
