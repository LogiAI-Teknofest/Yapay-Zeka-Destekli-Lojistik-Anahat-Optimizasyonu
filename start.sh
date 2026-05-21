#!/bin/bash
# LogiAI — Lojistik Optimizasyon Sistemi Hızlı Başlatma
# Proje kök dizininden çalıştırın: ./start.sh [docker]

set -e

echo "🚛 LogiAI Lojistik Optimizasyon Sistemi"
echo "════════════════════════════════════════"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Docker modu ──
if [ "$1" == "docker" ]; then
    echo "🐳 Docker Compose başlatılıyor..."
    docker compose up -d --build
    echo ""
    echo "✅ Servisler:"
    echo "   API:       http://localhost:8000"
    echo "   Dashboard: http://localhost:8501"
    echo "   API Docs:  http://localhost:8000/docs"
    exit 0
fi

# ── Yerel mod ──
echo "📦 Bağımlılıklar kontrol ediliyor..."

# API
if [ ! -d "src/app/venv_api" ]; then
    echo "🐍 API virtualenv oluşturuluyor..."
    python3 -m venv src/app/venv_api
    src/app/venv_api/bin/pip install -r src/app/requirements.txt
fi

# Dashboard
if [ ! -d "src/app/venv_dashboard" ]; then
    echo "🐍 Dashboard virtualenv oluşturuluyor..."
    python3 -m venv src/app/venv_dashboard
    src/app/venv_dashboard/bin/pip install -r src/app/dashboard_requirements.txt
fi

echo ""
echo "🚀 Servisler başlatılıyor..."

# API'yi arka planda başlat (DATA_DIR proje kökünden)
DATA_DIR="$SCRIPT_DIR/data/raw" OUTPUT_DIR="$SCRIPT_DIR/data/processed" \
    src/app/venv_api/bin/python -m uvicorn src.app.main:app --host 0.0.0.0 --port 8000 &
API_PID=$!
echo "   API başlatıldı (PID: $API_PID) → http://localhost:8000"

# Kısa bekleme
sleep 2

# Dashboard'ı ön planda başlat
echo "   Dashboard başlatılıyor → http://localhost:8501"
echo ""
echo "⏹️  Durdurmak için: Ctrl+C"
echo ""

# Cleanup trap
trap "echo ''; echo '🛑 Durduruluyor...'; kill $API_PID 2>/dev/null; exit 0" INT TERM

src/app/venv_dashboard/bin/streamlit run src/app/dashboard.py --server.port 8501

kill $API_PID 2>/dev/null
