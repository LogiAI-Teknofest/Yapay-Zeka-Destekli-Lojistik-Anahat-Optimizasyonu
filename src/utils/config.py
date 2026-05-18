"""
LogiAI — Merkezi konfigürasyon
Tüm modüller ortam değişkenlerini buradan okur.
"""

import os
import redis

# ─── Redis ────────────────────────────────────────────────────────────────────
REDIS_HOST: str = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT: int = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB: int = int(os.getenv("REDIS_DB", "0"))

# ─── Optimizer Monitoring ──────────────────────────────────────────────────────
OPTIMIZER_PENALTY_THRESHOLD: float = float(os.getenv("OPTIMIZER_PENALTY_THRESHOLD", "5000"))
PERSON_D_API_GATEWAY_URL: str = os.getenv("PERSON_D_API_GATEWAY_URL", "")
PERSON_D_API_KEY: str = os.getenv("PERSON_D_API_KEY", "")
ALERT_TIMEOUT_SECONDS: int = int(os.getenv("ALERT_TIMEOUT_SECONDS", "5"))

# ─── ML / LSTM ────────────────────────────────────────────────────────────────
LSTM_MODEL_PATH: str = os.getenv("LSTM_MODEL_PATH", "")

# ─── Dashboard / API ──────────────────────────────────────────────────────────
LOGIAI_API_URL: str = os.getenv("LOGIAI_API_URL", "http://localhost:8000")


def get_redis_client(decode_responses: bool = True) -> redis.Redis:
    """Konfigürasyona göre Redis istemcisi döndürür."""
    return redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        db=REDIS_DB,
        decode_responses=decode_responses,
    )
