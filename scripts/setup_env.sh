#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
# NEXUS v5.0 — Script de configuración del entorno local
# Ejecutar una sola vez al clonar el proyecto.
# Uso: bash scripts/setup_env.sh
# ═══════════════════════════════════════════════════════════

set -e

echo ""
echo "═══════════════════════════════════════════"
echo "  NEXUS v5.0 — Configuración de Entorno"
echo "═══════════════════════════════════════════"
echo ""

# 1. Crear venv si no existe
if [ ! -d ".venv" ]; then
    echo "📦 Creando entorno virtual .venv..."
    python3 -m venv .venv
    echo "✅ .venv creado"
else
    echo "✅ .venv ya existe — omitiendo creación"
fi

# 2. Activar y actualizar pip
echo "⚡ Activando entorno virtual..."
source .venv/bin/activate
echo "🔧 Actualizando pip, setuptools y wheel..."
pip install --upgrade pip setuptools wheel --quiet

# 3. Instalar dependencias
echo "📥 Instalando dependencias de requirements.txt..."
pip install -r requirements.txt

# 4. Verificar .env
if [ ! -f ".env" ]; then
    echo ""
    echo "⚠️  ARCHIVO .env NO ENCONTRADO"
    echo "   Ejecuta: cp .env.example .env"
    echo "   Luego completa tus credenciales reales."
else
    echo "✅ .env encontrado"
fi

echo ""
echo "═══════════════════════════════════════════"
echo "  ✅ Entorno listo. Para iniciar NEXUS:"
echo "     source .venv/bin/activate"
echo "     python main.py"
echo "═══════════════════════════════════════════"
echo ""
