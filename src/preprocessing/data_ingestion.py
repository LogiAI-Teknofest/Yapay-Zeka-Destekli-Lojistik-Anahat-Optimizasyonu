"""
preprocessing/data_ingestion.py
================================
Redis veri yükleme — data_validation re-export + Redis yükleyici.
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from .data_validation import validate_transfer_centers, validate_vehicles

logger = logging.getLogger(__name__)

__all__ = ["validate_transfer_centers", "validate_vehicles", "load_transfer_centers_to_redis"]


def load_transfer_centers_to_redis(df: pd.DataFrame, sm=None) -> dict:
    """
    TM satırlarını doğrulayıp Redis state'e yazar.

    Parameters
    ----------
    df : TM_ID ve Capacity sütunlu DataFrame
    sm : RedisStateManager örneği; None ise yeni örnek oluşturulur.

    Returns
    -------
    {"loaded": int, "skipped": int, "errors": list[str]}
    """
    clean, errors = validate_transfer_centers(df)
    if not clean:
        return {"loaded": 0, "skipped": 0, "errors": errors}

    if sm is None:
        from src.utils.state_manager import RedisStateManager
        sm = RedisStateManager()

    redis_client = sm.redis
    pipe = redis_client.pipeline()
    for row in clean:
        tm_id = row["TM_ID"]
        cap = row["Capacity"]
        pipe.hset(
            f"TM:{tm_id}:State",
            mapping={
                "MaxCapacity":    int(cap),
                "CurrentLoad":    0,
                "OverloadAmount": 0,
                "AcceptsTruck":   0,
            },
        )
    pipe.execute()
    logger.info("Redis'e %d TM yüklendi.", len(clean))
    return {"loaded": len(clean), "skipped": len(errors), "errors": errors}
