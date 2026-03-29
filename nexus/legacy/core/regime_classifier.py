"""
NEXUS Trading System — Market Regime Classifier
================================================
Analiza el entorno macro del mercado (Régimen) utilizando
aprendizaje no supervisado (K-Means Clustering) sobre 
características de volatilidad y tendencia.
"""

import logging
from typing import Dict, Any
import numpy as np  # type: ignore
import pandas as pd  # type: ignore
from ta.trend import ADXIndicator  # type: ignore
from ta.volatility import AverageTrueRange  # type: ignore
from sklearn.cluster import KMeans  # type: ignore
from sklearn.preprocessing import StandardScaler  # type: ignore

logger = logging.getLogger("nexus.regime_classifier")

class RegimeClassifier:
    """
    Clasificador Dinámico de Régimen de Mercado.
    Entrena un modelo K-Means on-the-fly usando una ventana móvil
    para detectar si el mercado está en tendencia, rango o pánico.
    """

    def __init__(self, window: int = 14, clusters: int = 4):
        self.window = window
        self.clusters = clusters
        self.scaler = StandardScaler()
        self.model = KMeans(n_clusters=self.clusters, random_state=42, n_init=10)

    def _prepare_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calcula las características para el clustering."""
        features = pd.DataFrame(index=df.index)
        
        # 1. Fuerza de Tendencia (ADX)
        try:
            adx_ind = ADXIndicator(high=df["high"], low=df["low"], close=df["close"], window=self.window)
            features["adx"] = adx_ind.adx()
            
            # Direccionalidad (+DI vs -DI) para saber si es Bull o Bear
            features["di_diff"] = adx_ind.adx_pos() - adx_ind.adx_neg()
        except Exception:
            features["adx"] = 0.0
            features["di_diff"] = 0.0
            
        # 2. Volatilidad (ATR normalizado por precio)
        try:
            atr_ind = AverageTrueRange(high=df["high"], low=df["low"], close=df["close"], window=self.window)
            features["atr_pct"] = atr_ind.average_true_range() / df["close"] * 100
        except Exception:
            features["atr_pct"] = 0.0

        return features.dropna()

    def _assign_labels_to_centroids(self, centroids: np.ndarray) -> Dict[int, str]:
        """
        Interpreta los centroides para asignar nombres legibles humanos al régimen.
        Index de columnas en centroides: [adx, di_diff, atr_pct]
        """
        labels = {}
        for idx, centroid in enumerate(centroids):
            c_adx, c_di_diff, c_atr = centroid[0], centroid[1], centroid[2]
            
            # Umbrales lógicos sobre los centroides estandarizados (z-scores)
            if c_adx > 0.5 and c_di_diff > 0.5:
                regime = "Strong Bull Trend"
            elif c_adx > 0.5 and c_di_diff < -0.5:
                regime = "Strong Bear Trend"
            elif c_atr > 1.0:
                regime = "High Volatility / Chopping"
            elif c_adx < -0.5 and c_atr < 0:
                regime = "Quiet Ranging Market"
            else:
                regime = "Transitional / Mixed"
                
            labels[idx] = regime
        return labels

    def detect_regime(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Detecta el régimen actual del mercado.
        
        Returns:
            Dict con 'regime' (str), 'adx' (float), 'atr_pct' (float)
        """
        if len(df) < self.window * 2:
            return {"regime": "Unknown (Not enough data)", "adx": 0.0, "atr_pct": 0.0}

        features = self._prepare_features(df)
        if len(features) < self.clusters:
             return {"regime": "Unknown (Dropna failed)", "adx": 0.0, "atr_pct": 0.0}

        # Extraemos matriz X
        X = features[["adx", "di_diff", "atr_pct"]].values
        X_scaled = self.scaler.fit_transform(X)

        # Entrenar K-Means con los datos recientes
        self.model.fit(X_scaled)
        
        # Etiquetar heurísticamente los centroides
        cluster_labels = self._assign_labels_to_centroids(self.model.cluster_centers_)
        
        # Predecir el cluster de la última vela
        current_features = X_scaled[-1].reshape(1, -1)
        current_cluster = self.model.predict(current_features)[0]
        
        current_regime_name = cluster_labels[current_cluster]
        
        return {
            "regime": current_regime_name,
            "adx": round(features["adx"].iloc[-1], 2),
            "atr_pct": round(features["atr_pct"].iloc[-1], 4),
            "di_diff": round(features["di_diff"].iloc[-1], 2)
        }
