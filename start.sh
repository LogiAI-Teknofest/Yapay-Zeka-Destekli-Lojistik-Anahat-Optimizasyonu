#!/bin/bash
# LogiAI — Lojistik Optimizasyon Sistemi Hizli Baslatma (Docker)
# Proje kok dizininden calistirin: ./start.sh
#
# NOT: Sistem yalnizca Docker uzerinden calisir. Calisma ortami (API + Dashboard
# + Redis) docker-compose ile ayaga kalkar; yerel/venv modu kaldirilmistir.
# Onrapor'daki konteyner ortami (4 CPU, 16 GB RAM) ile birebir ayni runtime.

set -e

echo "LogiAI Lojistik Optimizasyon Sistemi"
echo "========================================"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Docker kurulu mu kontrol et
if ! command -v docker >/dev/null 2>&1; then
    echo "HATA: Docker bulunamadi. Lutfen Docker Desktop / Docker Engine kurun."
    exit 1
fi

echo "Docker Compose baslatiliyor..."
docker compose up -d --build

echo ""
echo "Servisler ayaga kalkiyor:"
echo "   API:       http://localhost:8000"
echo "   Dashboard: http://localhost:8501"
echo "   API Docs:  http://localhost:8000/docs"
echo ""
echo "Loglari izlemek icin:  docker compose logs -f"
echo "Durdurmak icin:        docker compose down"
