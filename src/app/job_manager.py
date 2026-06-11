"""
job_manager.py
==============
Redis tabanlı async iş kuyruğu yöneticisi.

İş yaşam döngüsü: PENDING → RUNNING → COMPLETED | FAILED
TTL: 1 saat (3600 sn) — job tamamlanmış olsa bile sonuç o süre Redis'te kalır.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

import redis

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))
_JOB_PREFIX  = "logiai:job:"
_DATE_PREFIX = "logiai:date:"
_JOB_TTL = 3600


def _client() -> redis.Redis:
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _patch(job_id: str, updates: dict) -> None:
    r = _client()
    key = f"{_JOB_PREFIX}{job_id}"
    raw = r.get(key)
    if raw is None:
        return
    data = json.loads(raw)
    data.update(updates)
    r.setex(key, _JOB_TTL, json.dumps(data))


# ── Durum geçişleri ──────────────────────────────────────────────────────────

def create_job() -> str:
    job_id = str(uuid.uuid4())
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
    return job_id


def set_running(job_id: str) -> None:
    _patch(job_id, {"status": "RUNNING", "started_at": _now()})


def set_completed(job_id: str, result: dict) -> None:
    _patch(job_id, {"status": "COMPLETED", "finished_at": _now(), "result": result})
    # Tarih → job_id indexi: /api/fleet gibi endpoint'ler tarihe göre sonucu bulabilsin
    date = result.get("date")
    if date:
        _client().setex(f"{_DATE_PREFIX}{date}", _JOB_TTL, job_id)


def set_failed(job_id: str, error: str) -> None:
    _patch(job_id, {"status": "FAILED", "finished_at": _now(), "error": error})


# ── Sorgu ────────────────────────────────────────────────────────────────────

def get_job(job_id: str) -> dict | None:
    raw = _client().get(f"{_JOB_PREFIX}{job_id}")
    return json.loads(raw) if raw else None


def get_job_for_date(date: str) -> dict | None:
    """Verilen tarih için en son tamamlanmış job'u döner."""
    job_id = _client().get(f"{_DATE_PREFIX}{date}")
    if not job_id:
        return None
    return get_job(job_id)
