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

# ── API venv ──
if [ ! -d "src/app/venv_api" ]; then
    echo "🐍 API virtualenv oluşturuluyor..."
    python3 -m venv src/app/venv_api
fi

# Venv var ama uvicorn eksik olabilir — her zaman install çalıştır (pip idempotent)
echo "   📥 API bağımlılıkları yükleniyor..."
src/app/venv_api/bin/pip install --quiet --upgrade pip
src/app/venv_api/bin/pip install --quiet -r src/app/requirements.txt

# ── Dashboard venv ──
if [ ! -d "src/app/venv_dashboard" ]; then
    echo "🐍 Dashboard virtualenv oluşturuluyor..."
    python3 -m venv src/app/venv_dashboard
fi

echo "   📥 Dashboard bağımlılıkları yükleniyor..."
src/app/venv_dashboard/bin/pip install --quiet --upgrade pip
src/app/venv_dashboard/bin/pip install --quiet -r src/app/dashboard_requirements.txt

# ── Kontrol ──
if ! src/app/venv_api/bin/python -c "import uvicorn" 2>/dev/null; then
    echo "❌ uvicorn yüklenemedi, manuel kontrol gerekli."
    exit 1
fi

if ! src/app/venv_dashboard/bin/python -c "import streamlit" 2>/dev/null; then
    echo "❌ streamlit yüklenemedi, manuel kontrol gerekli."
    exit 1
fi

echo ""
echo "🚀 Servisler başlatılıyor..."

# API'yi arka planda başlat
DATA_DIR="$SCRIPT_DIR/data/raw" \
OUTPUT_DIR="$SCRIPT_DIR/data/processed" \
    src/app/venv_api/bin/python -m uvicorn src.app.main:app \
        --host 0.0.0.0 --port 8000 --workers 1 &
API_PID=$!
echo "   API başlatıldı (PID: $API_PID) → http://localhost:8000"

# API ayağa kalkana kadar bekle
echo "   ⏳ API hazır olana kadar bekleniyor..."
for i in $(seq 1 15); do
    sleep 1
    if src/app/venv_api/bin/python -c \
        "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')" \
        2>/dev/null; then
        echo "   ✅ API hazır."
        break
    fi
    if [ $i -eq 15 ]; then
        echo "   ⚠️  API 15sn içinde yanıt vermedi, dashboard yine de başlatılıyor..."
    fi
done

# Dashboard'ı ön planda başlat
echo "   Dashboard başlatılıyor → http://localhost:8501"
echo ""
echo "⏹️  Durdurmak için: Ctrl+C"
echo ""

# Cleanup trap
trap "echo ''; echo '🛑 Durduruluyor...'; kill $API_PID 2>/dev/null; exit 0" INT TERM

API_BASE="http://localhost:8000" \
    src/app/venv_dashboard/bin/streamlit run src/app/dashboard.py \
        --server.port 8501 \
        --server.address 0.0.0.0

kill $API_PID 2>/dev/null
