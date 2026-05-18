"""
LogiAI — Veri Dogrulama (Kisi C)
TM ve Arac verilerini Redis'e yazmadan once dogrular.
"""

import logging
import re
from typing import Dict, List, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

VALID_VEHICLE_TYPES    = {"Tır", "Kamyon", "Hafif Kamyon", "Kamyonet"}
_TM_ID_RE              = re.compile(r"^\d{2}_\d{2}$")
_TM_REQUIRED_COLS      = {"TM_ID", "Capacity"}
_VEHICLE_REQUIRED_COLS = {"Vehicle_ID", "Type", "Capacity"}
MAX_SINGLE_DESI        = 3000   # En büyük araç (TIR_01) kapasitesi


def validate_transfer_centers(df: pd.DataFrame) -> Tuple[pd.DataFrame, List[str]]:
    """
    TM verisi dogrular: TM_ID format, Capacity > 0, duplikat.
    Donus: (temiz DataFrame, hata mesajlari listesi)
    """
    errors: List[str] = []

    missing_cols = _TM_REQUIRED_COLS - set(df.columns)
    if missing_cols:
        raise ValueError(f"DataFrame'de eksik zorunlu sutunlar: {sorted(missing_cols)}")

    mask = pd.Series(True, index=df.index)

    invalid_id = ~df["TM_ID"].astype(str).str.match(_TM_ID_RE)
    for tm_id in df.loc[invalid_id, "TM_ID"]:
        errors.append(f"Gecersiz TM_ID formati: '{tm_id}' (beklenen: 34_01)")
    mask &= ~invalid_id

    neg_cap = df["Capacity"].isna() | (df["Capacity"] <= 0)
    for tm_id in df.loc[neg_cap, "TM_ID"]:
        errors.append(f"Gecersiz kapasite (NaN veya <= 0): TM '{tm_id}'")
    mask &= ~neg_cap

    dupes = df.duplicated(subset=["TM_ID"], keep="first")
    for tm_id in df.loc[dupes, "TM_ID"]:
        errors.append(f"Duplikat TM_ID: '{tm_id}'")
    mask &= ~dupes

    clean = df[mask].reset_index(drop=True)
    if errors:
        logger.warning(f"TM validasyon: {len(errors)} hata, {len(clean)} gecerli kayit")
    return clean, errors


def validate_vehicles(df: pd.DataFrame) -> Tuple[pd.DataFrame, List[str]]:
    """
    Arac verisi dogrular: Vehicle_ID, Type (gecerli set), Capacity > 0, duplikat.
    Donus: (temiz DataFrame, hata mesajlari listesi)
    """
    errors: List[str] = []
    mask = pd.Series(True, index=df.index)

    invalid_type = ~df["Type"].isin(VALID_VEHICLE_TYPES)
    for _, row in df[invalid_type].iterrows():
        errors.append(
            f"Gecersiz arac tipi '{row['Type']}': Vehicle '{row['Vehicle_ID']}' "
            f"(gecerli: {sorted(VALID_VEHICLE_TYPES)})"
        )
    mask &= ~invalid_type

    neg_cap = df["Capacity"] <= 0
    for v_id in df.loc[neg_cap, "Vehicle_ID"]:
        errors.append(f"Gecersiz kapasite (<= 0): Vehicle '{v_id}'")
    mask &= ~neg_cap

    dupes = df.duplicated(subset=["Vehicle_ID"], keep="first")
    for v_id in df.loc[dupes, "Vehicle_ID"]:
        errors.append(f"Duplikat Vehicle_ID: '{v_id}'")
    mask &= ~dupes

    clean = df[mask].reset_index(drop=True)
    if errors:
        logger.warning(f"Arac validasyon: {len(errors)} hata, {len(clean)} gecerli kayit")
    return clean, errors


def validate_packages(packages: List[Dict]) -> Tuple[List[Dict], List[str]]:
    """
    Paket listesini dogrular: pkg_id, tm_id, desi > 0.
    """
    from src.utils.state_manager import TM_MAX_CAP

    errors: List[str] = []
    clean  = []
    seen_ids = set()

    for pkg in packages:
        pkg_id = pkg.get("pkg_id", "?")
        tm_id  = pkg.get("tm_id")
        desi   = pkg.get("desi", 0)
        ok     = True

        if not pkg_id or pkg_id in seen_ids:
            errors.append(f"Duplikat veya eksik pkg_id: '{pkg_id}'")
            ok = False
        if tm_id not in TM_MAX_CAP:
            errors.append(f"Tanimli olmayan TM: '{tm_id}' (pkg: {pkg_id})")
            ok = False
        if not isinstance(desi, (int, float)) or isinstance(desi, bool):
            errors.append(f"Sayisal olmayan desi '{desi}' ({type(desi).__name__}) (pkg: {pkg_id})")
            ok = False
        elif desi <= 0:
            errors.append(f"Sifir veya negatif desi '{desi}' (pkg: {pkg_id})")
            ok = False
        elif desi > MAX_SINGLE_DESI:
            errors.append(
                f"Astronomik desi '{desi}' max siniri asan: {pkg_id} "
                f"(limit={MAX_SINGLE_DESI})"
            )
            ok = False

        if ok:
            seen_ids.add(pkg_id)
            clean.append(pkg)

    if errors:
        logger.warning(f"Paket validasyon: {len(errors)} hata, {len(clean)} gecerli")
    return clean, errors
