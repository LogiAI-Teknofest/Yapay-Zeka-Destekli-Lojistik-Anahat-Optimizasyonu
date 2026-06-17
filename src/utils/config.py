"""
LogiAI — Merkezi konfigürasyon
Tüm modüller ortam değişkenlerini buradan okur.
"""

import os

# ─── Redis ────────────────────────────────────────────────────────────────────
REDIS_HOST: str = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT: int = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB: int = int(os.getenv("REDIS_DB", "0"))

# ─── Optimizer ────────────────────────────────────────────────────────────────
OPTIMIZER_PENALTY_THRESHOLD: float = float(os.getenv("OPTIMIZER_PENALTY_THRESHOLD", "5000"))

# ─── Dashboard / API ──────────────────────────────────────────────────────────
LOGIAI_API_URL: str = os.getenv("LOGIAI_API_URL", "http://localhost:8000")
