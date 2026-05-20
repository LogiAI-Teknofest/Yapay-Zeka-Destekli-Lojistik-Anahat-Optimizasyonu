# ══════════════════════════════════════════════════════════════
#  LogiAI — Multi-Stage Dockerfile
#  Stage 1 (base)      : Ortak Python ortamı
#  Stage 2 (api)       : FastAPI + Uvicorn backend
#  Stage 3 (dashboard) : Streamlit + Folium KDS
# ══════════════════════════════════════════════════════════════

# ── Ortak temel ──────────────────────────────────────────────
FROM python:3.11-slim AS base

# Sistem bağımlılıkları (openpyxl için lxml, pandas için gcc)
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python bytecode üretme ve buffering'i kapat
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1


# ── API Stage ─────────────────────────────────────────────────
FROM base AS api

# Bağımlılıkları önce kopyala (layer cache)
COPY src/app/requirements.txt /tmp/api_requirements.txt
RUN pip install --no-cache-dir -r /tmp/api_requirements.txt

# Proje kaynak kodunu kopyala
COPY src/ /app/src/
COPY data/ /app/data/

# Çıktı dizinini oluştur
RUN mkdir -p /app/data/processed

# Ortam değişkenleri (docker-compose üzerine yazabilir)
ENV DATA_DIR=/app/data/raw \
    OUTPUT_DIR=/app/data/processed \
    REDIS_HOST=redis \
    REDIS_PORT=6379

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "src.app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", "--reload"]


# ── Dashboard Stage ───────────────────────────────────────────
FROM base AS dashboard

COPY src/app/dashboard_requirements.txt /tmp/dashboard_requirements.txt
RUN pip install --no-cache-dir -r /tmp/dashboard_requirements.txt

COPY src/app/dashboard.py /app/src/app/dashboard.py

ENV API_BASE=http://api:8000

EXPOSE 8501

# Streamlit için gerekli ayarlar
CMD ["streamlit", "run", "src/app/dashboard.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
