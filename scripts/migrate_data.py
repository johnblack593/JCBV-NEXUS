"""
Script de Migración de Datos y Estado NEXUS 
Migra configuraciones Legacy (Paramétrico, Scalers Globales, CB Volátil)
hacia el nuevo estándar institucional persistente.
"""

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

def migrate_cb_state():
    """Migrate in-memory CB behavior to the new JSON-based persistent state."""
    print("🔄 Migrando estado del Circuit Breaker...")
    cb_path = Path("logs/cb_state.json")
    if not cb_path.parent.exists():
        cb_path.parent.mkdir(parents=True)
        
    if cb_path.exists():
        print("✅ cb_state.json ya existe. Verificando integridad...")
        try:
            with open(cb_path, "r") as f:
                state = json.load(f)
            if "until_timestamp" not in state:
                state["until_timestamp"] = time.time()
            print("✅ cb_state validado satisfactoriamente.")
        except Exception as e:
            print(f"⚠️ cb_state corrupto ({e}). Recreando limpio...")
            cb_path.unlink(missing_ok=True)
    
    if not cb_path.exists():
        state = {
            "is_active": False,
            "until_timestamp": 0.0,
            "activated_at": datetime.now(timezone.utc).isoformat(),
            "migration_note": "Migrado desde Memory-Only a JSON"
        }
        with open(cb_path, "w") as f:
            json.dump(state, f, indent=2)
        print("✅ Nuevo archivo cb_state.json persistente creado con estado limpio (False).")

def migrate_database_schemas():
    """Verifica si database necesita nuevos campos para el Historical VaR."""
    db_path = Path("nexus_data.db")
    if not db_path.exists():
        print("⏭️ Base de datos SQLite no detectada. Omitiendo migración DB.")
        return
        
    print("🔄 Migrando estructura SQLite (Nexus Data)...")
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Inyectando tabla de Historical VaR Si no existe
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS risk_historical_var (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                var_percent REAL,
                cvar_percent REAL,
                lookback_days INTEGER
            )
        ''')
        
        # Validar si existe la métrica de Circuit Breaker events
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS circuit_breaker_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                drawdown_pct REAL,
                blocked_until REAL
            )
        ''')
        
        conn.commit()
        conn.close()
        print("✅ Base de datos SQLite actualizada correctamente para el nuevo Risk Manager.")
    except Exception as e:
        print(f"❌ Error migrando base de datos: {e}")

def purge_legacy_scalers():
    """Purge outdated global scalers to force Windowed Scalers generation on next run."""
    print("🔄 Purgando caché de modelos ML y scalers con Lookahead Bias...")
    models_dir = Path("models")
    if not models_dir.exists():
        print("⏭️ Directorio models/ no encontrado.")
        return
        
    purged = 0
    for root, _, files in os.walk(models_dir):
        for file in files:
            if "scaler" in file.lower() or file.endswith(".pkl") or file.endswith(".keras"):
                file_path = Path(root) / file
                file_path.unlink()
                print(f"   🗑️ Eliminado: {file}")
                purged += 1
                
    if purged > 0:
        print(f"✅ {purged} modelos/scalers legacy purgados. El sistema usará Windowed Scalers (Sin Fuga).")
    else:
        print("✅ Ningún scaler contaminado detectado.")

if __name__ == "__main__":
    print("="*60)
    print("🚀 NEXUS: MIGRACIÓN DE ENTORNO INSTITUCIONAL V3")
    print("="*60)
    migrate_cb_state()
    migrate_database_schemas()
    purge_legacy_scalers()
    print("="*60)
    print("🎉 Migración completada! Sistema purificado y listo para arrancar en el Event Loop.")
    print("="*60)
