# ═══════════════════════════════════════════════════════════
# NEXUS v5.0 — Script de configuración del entorno local
# Ejecutar una sola vez al clonar el proyecto.
# ═══════════════════════════════════════════════════════════
# Uso:
#   1. Abre PowerShell en la raíz del proyecto
#   2. Ejecuta: .\scripts\setup_env.ps1
# ═══════════════════════════════════════════════════════════

Write-Host ""
Write-Host "═══════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  NEXUS v5.0 — Configuración de Entorno"    -ForegroundColor Cyan
Write-Host "═══════════════════════════════════════════" -ForegroundColor Cyan
Write-Host ""

# 1. Verificar Python
$pythonVersion = python --version 2>&1
Write-Host "🐍 Python encontrado: $pythonVersion"

# 2. Crear venv si no existe
if (-Not (Test-Path ".venv")) {
    Write-Host "📦 Creando entorno virtual .venv..." -ForegroundColor Yellow
    python -m venv .venv
    Write-Host "✅ .venv creado" -ForegroundColor Green
} else {
    Write-Host "✅ .venv ya existe — omitiendo creación" -ForegroundColor Green
}

# 3. Activar venv
Write-Host "⚡ Activando entorno virtual..." -ForegroundColor Yellow
& .\.venv\Scripts\Activate.ps1

# 4. Actualizar pip
Write-Host "🔧 Actualizando pip, setuptools y wheel..." -ForegroundColor Yellow
python -m pip install --upgrade pip setuptools wheel --quiet

# 5. Instalar dependencias
Write-Host "📥 Instalando dependencias de requirements.txt..." -ForegroundColor Yellow
pip install -r requirements.txt

# 6. Verificar .env
if (-Not (Test-Path ".env")) {
    Write-Host ""
    Write-Host "⚠️  ARCHIVO .env NO ENCONTRADO" -ForegroundColor Red
    Write-Host "   Copia .env.example a .env y completa tus credenciales:" -ForegroundColor Yellow
    Write-Host "   cp .env.example .env" -ForegroundColor White
} else {
    Write-Host "✅ .env encontrado" -ForegroundColor Green
}

Write-Host ""
Write-Host "═══════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  ✅ Entorno listo. Para iniciar NEXUS:"    -ForegroundColor Green
Write-Host "     .\.venv\Scripts\Activate.ps1"          -ForegroundColor White
Write-Host "     python main.py"                         -ForegroundColor White
Write-Host "═══════════════════════════════════════════" -ForegroundColor Cyan
Write-Host ""
