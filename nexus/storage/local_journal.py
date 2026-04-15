"""
NEXUS v5.0 — Local Journal (persistencia degradada sin QuestDB)
================================================================
Cuando QuestDB está offline, NEXUS persiste trades, equity y estado
de sesión en archivos locales (CSV + JSON) para no perder datos
entre reinicios.

Archivos:
    data/journal/trades.csv    — registro de operaciones
    data/journal/equity.csv    — balance post-trade
    data/journal/state.json    — estado de sesión actual
    data/journal/archive/      — CSVs rotados (>500 filas)

Reglas:
    - Nunca lanza excepciones fatales (todo → log warning)
    - Escrituras tipo append (excepto state.json que se sobreescribe)
    - Headers se agregan solo en la primera fila
    - Usa csv.DictWriter / csv.DictReader
    - Lock asyncio para escrituras concurrentes
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nexus.storage.journal")

# Timezone GMT-5 (Colombia / EST)
_TZ_GMT5 = timezone(timedelta(hours=-5))

# ── CSV Column Definitions ────────────────────────────────────────────
_TRADE_COLUMNS = [
    "timestamp", "asset", "direction", "size", "payout_pct",
    "outcome", "pnl", "balance", "venue",
]

_EQUITY_COLUMNS = ["timestamp", "balance"]

# ── Rotation Threshold ────────────────────────────────────────────────
_MAX_ROWS_BEFORE_ROTATE = 500


class LocalJournal:
    """
    Persistencia local ligera para operaciones NEXUS.

    Uso:
        journal = LocalJournal()
        journal.log_trade({...})
        journal.log_equity(balance)
        journal.save_session_state(pnl, trades, balance)
        state = journal.load_session_state()
    """

    def __init__(self, base_dir: str = "data/journal") -> None:
        self._base_dir = Path(base_dir)
        self._archive_dir = self._base_dir / "archive"
        self._trades_path = self._base_dir / "trades.csv"
        self._equity_path = self._base_dir / "equity.csv"
        self._state_path = self._base_dir / "state.json"
        self._lock = asyncio.Lock()

        # Crear directorios si no existen
        try:
            self._base_dir.mkdir(parents=True, exist_ok=True)
            self._archive_dir.mkdir(parents=True, exist_ok=True)
            logger.info("📂 LocalJournal inicializado: %s", self._base_dir)
        except Exception as exc:
            logger.warning("No se pudo crear directorio journal: %s", exc)

        # Rotación al inicializar
        self._rotate_if_needed()

    # ══════════════════════════════════════════════════════════════════
    #  Trades
    # ══════════════════════════════════════════════════════════════════

    def log_trade(self, trade: Dict[str, Any]) -> None:
        """
        Agrega una fila al trades.csv con los datos del trade.

        Args:
            trade: Dict con claves: timestamp, asset, direction, size,
                   payout_pct, outcome, pnl, balance, venue
        """
        try:
            needs_header = not self._trades_path.exists() or self._trades_path.stat().st_size == 0

            with open(self._trades_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=_TRADE_COLUMNS, extrasaction="ignore")
                if needs_header:
                    writer.writeheader()

                # Ensure timestamp exists
                if "timestamp" not in trade or not trade["timestamp"]:
                    trade["timestamp"] = datetime.now(_TZ_GMT5).isoformat()

                writer.writerow(trade)

            logger.debug("📝 Trade registrado en journal: %s %s",
                         trade.get("asset", "?"), trade.get("outcome", "?"))
        except Exception as exc:
            logger.warning("Error escribiendo trade en journal: %s", exc)

    # ══════════════════════════════════════════════════════════════════
    #  Equity
    # ══════════════════════════════════════════════════════════════════

    def log_equity(self, balance: float, timestamp: Optional[str] = None) -> None:
        """
        Agrega una fila a equity.csv con el balance actual.

        Args:
            balance: Balance actual después del trade.
            timestamp: ISO timestamp (auto-generado si None).
        """
        try:
            needs_header = not self._equity_path.exists() or self._equity_path.stat().st_size == 0

            with open(self._equity_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=_EQUITY_COLUMNS)
                if needs_header:
                    writer.writeheader()

                ts = timestamp or datetime.now(_TZ_GMT5).isoformat()
                writer.writerow({"timestamp": ts, "balance": balance})

            logger.debug("📊 Equity registrado: $%.2f", balance)
        except Exception as exc:
            logger.warning("Error escribiendo equity en journal: %s", exc)

    # ══════════════════════════════════════════════════════════════════
    #  Session State (sobreescribe)
    # ══════════════════════════════════════════════════════════════════

    def save_session_state(
        self,
        session_pnl: float,
        trades_today: int,
        balance: float,
    ) -> None:
        """
        Escribe/sobreescribe state.json con el estado de sesión actual.
        """
        try:
            state = {
                "session_pnl": round(session_pnl, 4),
                "trades_today": trades_today,
                "balance": round(balance, 2),
                "last_updated": datetime.now(_TZ_GMT5).isoformat(),
            }
            # Write to temp file first, then rename (atomic on most OS)
            tmp_path = self._state_path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
            tmp_path.replace(self._state_path)

            logger.debug("💾 Estado de sesión guardado: trades=%d, P&L=$%.2f",
                         trades_today, session_pnl)
        except Exception as exc:
            logger.warning("Error guardando estado de sesión: %s", exc)

    def load_session_state(self) -> Dict[str, Any]:
        """
        Carga state.json si existe.
        Si no existe o está corrupto, devuelve estado vacío.
        """
        default = {
            "session_pnl": 0.0,
            "trades_today": 0,
            "balance": 0.0,
            "last_updated": "",
        }
        try:
            if not self._state_path.exists():
                return default

            with open(self._state_path, "r", encoding="utf-8") as f:
                state = json.load(f)

            # Validate required keys
            if not isinstance(state, dict):
                logger.warning("state.json corrupto (no es dict). Usando defaults.")
                return default

            # Merge with default to fill missing keys
            for key, val in default.items():
                if key not in state:
                    state[key] = val

            return state

        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("state.json corrupto: %s. Usando defaults.", exc)
            return default
        except Exception as exc:
            logger.warning("Error leyendo estado de sesión: %s", exc)
            return default

    # ══════════════════════════════════════════════════════════════════
    #  Weekly Queries
    # ══════════════════════════════════════════════════════════════════

    def get_weekly_trades(self) -> List[Dict[str, Any]]:
        """
        Lee trades.csv y devuelve solo los trades de los últimos 7 días.
        Si el archivo no existe, devuelve [].
        """
        try:
            if not self._trades_path.exists():
                return []

            cutoff = datetime.now(_TZ_GMT5) - timedelta(days=7)
            trades: List[Dict[str, Any]] = []

            with open(self._trades_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # Parse timestamp and filter by date
                    ts_str = row.get("timestamp", "")
                    try:
                        # Try ISO format
                        ts = datetime.fromisoformat(ts_str)
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=_TZ_GMT5)
                        if ts < cutoff:
                            continue
                    except (ValueError, TypeError):
                        # If timestamp can't be parsed, include the trade anyway
                        pass

                    # Convert numeric fields
                    parsed = dict(row)
                    for key in ("size", "payout_pct", "pnl", "balance"):
                        if key in parsed and parsed[key]:
                            try:
                                parsed[key] = float(parsed[key])
                            except (ValueError, TypeError):
                                pass
                    trades.append(parsed)

            return trades

        except Exception as exc:
            logger.warning("Error leyendo trades del journal: %s", exc)
            return []

    def get_weekly_equity(self) -> List[float]:
        """
        Lee equity.csv y devuelve balances de los últimos 7 días.
        Si el archivo no existe, devuelve [].
        """
        try:
            if not self._equity_path.exists():
                return []

            cutoff = datetime.now(_TZ_GMT5) - timedelta(days=7)
            balances: List[float] = []

            with open(self._equity_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    ts_str = row.get("timestamp", "")
                    try:
                        ts = datetime.fromisoformat(ts_str)
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=_TZ_GMT5)
                        if ts < cutoff:
                            continue
                    except (ValueError, TypeError):
                        pass

                    bal_str = row.get("balance", "")
                    if bal_str:
                        try:
                            balances.append(float(bal_str))
                        except (ValueError, TypeError):
                            pass

            return balances

        except Exception as exc:
            logger.warning("Error leyendo equity del journal: %s", exc)
            return []

    # ══════════════════════════════════════════════════════════════════
    #  Summary (for startup report)
    # ══════════════════════════════════════════════════════════════════

    def get_trade_count(self) -> int:
        """Cuenta total de trades en el CSV actual (sin filtrar por fecha)."""
        try:
            if not self._trades_path.exists():
                return 0
            with open(self._trades_path, "r", encoding="utf-8") as f:
                # Subtract 1 for header
                count = sum(1 for _ in f) - 1
                return max(0, count)
        except Exception:
            return 0

    # ══════════════════════════════════════════════════════════════════
    #  Rotation
    # ══════════════════════════════════════════════════════════════════

    def _rotate_if_needed(self) -> None:
        """
        Rota trades.csv si tiene más de _MAX_ROWS_BEFORE_ROTATE filas.
        Mueve el archivo actual a data/journal/archive/trades_YYYYMMDD.csv
        y crea uno nuevo vacío.
        """
        try:
            if not self._trades_path.exists():
                return

            row_count = self.get_trade_count()
            if row_count <= _MAX_ROWS_BEFORE_ROTATE:
                return

            # Generate archive filename
            date_tag = datetime.now(_TZ_GMT5).strftime("%Y%m%d_%H%M%S")
            archive_name = f"trades_{date_tag}.csv"
            archive_path = self._archive_dir / archive_name

            shutil.move(str(self._trades_path), str(archive_path))
            logger.info(
                "📂 Journal rotado: %d trades → %s",
                row_count, archive_name
            )

            # Also rotate equity if it's large
            if self._equity_path.exists():
                eq_count = 0
                with open(self._equity_path, "r", encoding="utf-8") as f:
                    eq_count = sum(1 for _ in f) - 1
                if eq_count > _MAX_ROWS_BEFORE_ROTATE:
                    eq_archive = self._archive_dir / f"equity_{date_tag}.csv"
                    shutil.move(str(self._equity_path), str(eq_archive))
                    logger.info("📂 Equity rotado: %d registros → %s", eq_count, eq_archive.name)

        except Exception as exc:
            logger.warning("Error en rotación del journal: %s", exc)

    # ══════════════════════════════════════════════════════════════════
    #  Status
    # ══════════════════════════════════════════════════════════════════

    def get_status(self) -> str:
        """Retorna cadena de estado para el reporte de infraestructura."""
        try:
            self._base_dir.mkdir(parents=True, exist_ok=True)
            count = self.get_trade_count()
            return f"OK:{count} trades en {self._trades_path}"
        except Exception as exc:
            return f"ERROR:{exc}"

    def __repr__(self) -> str:
        count = self.get_trade_count()
        return f"<LocalJournal trades={count} path={self._base_dir}>"
