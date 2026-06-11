"""
preprocessing/data_validation.py
=================================
Veri giriş validasyonu — transfer merkezi, araç ve paket doğrulama.
"""

from __future__ import annotations

import re
from typing import Any

import pandas as pd

MAX_SINGLE_DESI: int = 22400

_TM_ID_RE = re.compile(r"^\d{2,}_\d{2,}$")

_KNOWN_TM_IDS: frozenset[str] = frozenset({
    "34_01", "06_01", "35_01", "07_01",
})

_VALID_VEHICLE_TYPES: frozenset[str] = frozenset({
    "Tir", "Tır", "Kamyon", "Hafif Kamyon", "Kamyonet",
})


def validate_transfer_centers(
    df: pd.DataFrame,
) -> tuple[list[dict], list[str]]:
    """
    TM satırlarını doğrular.

    Kurallar:
      - TM_ID ve Capacity sütunları zorunlu (yoksa ValueError).
      - TM_ID: \d{2,}_\d{2,} biçiminde olmalı.
      - Capacity > 0.
      - Aynı TM_ID tekrar edemez.

    Returns
    -------
    (clean_rows, error_messages)
    """
    required = {"TM_ID", "Capacity"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Eksik zorunlu sütunlar: {sorted(missing)}")

    clean: list[dict] = []
    errors: list[str] = []
    seen: set[str] = set()

    for _, row in df.iterrows():
        tm_id = row["TM_ID"]
        cap_raw = row["Capacity"]

        if not isinstance(tm_id, str) or not _TM_ID_RE.match(tm_id):
            errors.append(f"Geçersiz TM_ID formatı: {tm_id!r}")
            continue

        if tm_id in seen:
            errors.append(f"Duplikat TM_ID: {tm_id!r}")
            continue

        try:
            cap = float(cap_raw) if cap_raw is not None else 0.0
        except (TypeError, ValueError):
            errors.append(f"Geçersiz kapasite: TM_ID={tm_id!r}, Capacity={cap_raw!r}")
            continue

        if cap <= 0:
            errors.append(f"Negatif/sıfır kapasite: TM_ID={tm_id!r}, Capacity={cap_raw}")
            continue

        seen.add(tm_id)
        clean.append({"TM_ID": tm_id, "Capacity": cap})

    return clean, errors


def validate_vehicles(
    df: pd.DataFrame,
) -> tuple[list[dict], list[str]]:
    """
    Araç satırlarını doğrular.

    Kurallar:
      - Vehicle_ID, Type, Capacity sütunları zorunlu.
      - Type: _VALID_VEHICLE_TYPES içinde olmalı.
      - Capacity > 0.
      - Aynı Vehicle_ID tekrar edemez.

    Returns
    -------
    (clean_rows, error_messages)
    """
    clean: list[dict] = []
    errors: list[str] = []
    seen: set[str] = set()

    for _, row in df.iterrows():
        vid = row["Vehicle_ID"]
        vtype = row["Type"]
        cap_raw = row["Capacity"]

        if vid in seen:
            errors.append(f"Duplikat Vehicle_ID: {vid!r}")
            continue

        if vtype not in _VALID_VEHICLE_TYPES:
            errors.append(f"Geçersiz araç tipi: Vehicle_ID={vid!r}, Type={vtype!r}")
            continue

        try:
            cap = float(cap_raw)
        except (TypeError, ValueError):
            errors.append(f"Geçersiz kapasite: Vehicle_ID={vid!r}, Capacity={cap_raw!r}")
            continue

        if cap <= 0:
            errors.append(f"Negatif/sıfır kapasite: Vehicle_ID={vid!r}, Capacity={cap_raw}")
            continue

        seen.add(vid)
        clean.append({"Vehicle_ID": vid, "Type": vtype, "Capacity": cap})

    return clean, errors


def validate_packages(
    packages: list[dict[str, Any]],
) -> tuple[list[dict], list[str]]:
    """
    Paket listesini doğrular.

    Kurallar:
      - desi sayısal, > 0, <= MAX_SINGLE_DESI.
      - tm_id _KNOWN_TM_IDS içinde olmalı.
      - pkg_id tekrar edemez.

    Returns
    -------
    (clean_packages, error_messages)
    """
    clean: list[dict] = []
    errors: list[str] = []
    seen: set[str] = set()

    for pkg in packages:
        pkg_id = pkg.get("pkg_id")
        tm_id = pkg.get("tm_id")
        desi_raw = pkg.get("desi")

        if pkg_id in seen:
            errors.append(f"Duplikat pkg_id: {pkg_id!r}")
            continue

        if tm_id not in _KNOWN_TM_IDS:
            errors.append(f"Bilinmeyen TM: pkg_id={pkg_id!r}, tm_id={tm_id!r}")
            continue

        try:
            desi = float(desi_raw)
        except (TypeError, ValueError):
            errors.append(f"Geçersiz desi: pkg_id={pkg_id!r}, desi={desi_raw!r}")
            continue

        if desi <= 0:
            errors.append(f"Sıfır/negatif desi: pkg_id={pkg_id!r}, desi={desi_raw}")
            continue

        if desi > MAX_SINGLE_DESI:
            errors.append(
                f"Maksimum desi aşıldı: pkg_id={pkg_id!r}, "
                f"desi={desi_raw} > {MAX_SINGLE_DESI}"
            )
            continue

        seen.add(pkg_id)
        clean.append(pkg)

    return clean, errors
