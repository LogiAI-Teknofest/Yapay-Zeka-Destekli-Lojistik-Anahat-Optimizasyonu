"""
job_manager.py
==============
Redis tabanlı async iş kuyruğu yöneticisi.

İş yaşam döngüsü: PENDING → RUNNING → COMPLETED | FAILED
TTL: 1 saat (3600 sn) — job tamamlanmış olsa bile sonuç o süre Redis'te kalır.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone

import redis

logger = logging.getLogger(__name__)

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))
_JOB_PREFIX  = "logiai:job:"
_DATE_PREFIX = "logiai:date:"
_JOB_TTL = 3600
_MAX_RETRIES = 3  # _patch() optimistic retry sayısı

# FIX #47 — max_connections eklendi
_pool = redis.ConnectionPool(
    host=REDIS_HOST,
    port=REDIS_PORT,
    decode_responses=True,
    max_connections=20,
)


def _client() -> redis.Redis:
    return redis.Redis(connection_pool=_pool)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _patch(job_id: str, updates: dict) -> None:
    """
    FIX #14 — Atomik WATCH/MULTI/EXEC pipeline ile race condition giderildi.
    Önceki Read-Modify-Write zinciri aynı anda iki worker tarafından bozulabiliyordu.
    """
    key = f"{_JOB_PREFIX}{job_id}"
    r = _client()
    for attempt in range(_MAX_RETRIES):
        try:
            with r.pipeline() as pipe:
                pipe.watch(key)
                raw = pipe.get(key)
                if raw is None:
                    pipe.unwatch()
                    return
                data = json.loads(raw)
                data.update(updates)
                pipe.multi()
                pipe.setex(key, _JOB_TTL, json.dumps(data))
                pipe.execute()
                return  # başarılı
        except redis.WatchError:
            # Başka bir worker aynı anda yazdı — yeniden dene
            logger.debug("_patch WatchError (attempt %d/%d) job_id=%s", attempt + 1, _MAX_RETRIES, job_id)
            continue
        except redis.RedisError as exc:  # FIX #41
            logger.error("Redis error in _patch: %s", exc)
            return
    logger.warning("_patch: max retries (%d) aşıldı, job_id=%s", _MAX_RETRIES, job_id)


# ── Durum geçişleri ──────────────────────────────────────────────────────────

def create_job() -> str:
    """FIX #41 — RedisError koruması eklendi."""
    job_id = str(uuid.uuid4())
    try:
        _client().setex(
            f"{_JOB_PREFIX}{job_id}",
            _JOB_TTL,
            json.dumps({
                "status": "PENDING",
                "created_at": _now(),
                "started_at": None,
                "finished_at": None,
                "result": None,
                "error": None,
            }),
        )
    except redis.RedisError as exc:
        logger.error("Redis error in create_job: %s", exc)
    return job_id


def set_running(job_id: str) -> None:
    """FIX #41 — RedisError koruması eklendi."""
    try:
        _patch(job_id, {"status": "RUNNING", "started_at": _now()})
    except redis.RedisError as exc:
        logger.error("Redis error in set_running: %s", exc)


def set_completed(job_id: str, result: dict) -> None:
    """
    FIX #13 — Tarih bazlı indeks artık eski job'ı silmek yerine RPUSH ile
    listenin sonuna ekleniyor. En son tamamlanan job her zaman listenin sonu.
    FIX #41 — RedisError koruması eklendi.
    FIX #40 — RPUSH sonrası LTRIM ile liste yalnızca son job_id'yi tutar;
    aynı tarih tekrar optimize edildiğinde liste sınırsız büyümez
    (get_job_for_date zaten yalnızca son elemanı kullanıyor).
    """
    try:
        _patch(job_id, {"status": "COMPLETED", "finished_at": _now(), "result": result})
        date = result.get("date")
        if date:
            r = _client()
            list_key = f"{_DATE_PREFIX}{date}"
            # RPUSH: listeye ekle (override yok), ardından sadece son elemanı tut
            r.rpush(list_key, job_id)
            r.ltrim(list_key, -1, -1)  # FIX #40 — yalnızca en güncel job_id kalsın
            r.expire(list_key, _JOB_TTL)
    except redis.RedisError as exc:
        logger.error("Redis error in set_completed: %s", exc)


def set_failed(job_id: str, error: str) -> None:
    """FIX #41 — RedisError koruması eklendi."""
    try:
        _patch(job_id, {"status": "FAILED", "finished_at": _now(), "error": error})
    except redis.RedisError as exc:
        logger.error("Redis error in set_failed: %s", exc)


# ── Sorgu ────────────────────────────────────────────────────────────────────

def get_job(job_id: str) -> dict | None:
    """FIX #41 — RedisError koruması eklendi."""
    try:
        raw = _client().get(f"{_JOB_PREFIX}{job_id}")
        return json.loads(raw) if raw else None
    except redis.RedisError as exc:
        logger.error("Redis error in get_job: %s", exc)
        return None


def get_job_for_date(date: str) -> dict | None:
    """
    Verilen tarih için en son tamamlanmış job'u döner.
    FIX #13 — LINDEX -1 ile listenin son (en güncel) elemanı alınıyor.
    FIX #41 — RedisError koruması eklendi.
    """
    try:
        r = _client()
        list_key = f"{_DATE_PREFIX}{date}"
        # En son eklenen job_id (listenin sonu)
        job_id = r.lindex(list_key, -1)
        if not job_id:
            return None
        return get_job(job_id)
    except redis.RedisError as exc:
        logger.error("Redis error in get_job_for_date: %s", exc)
        return None
