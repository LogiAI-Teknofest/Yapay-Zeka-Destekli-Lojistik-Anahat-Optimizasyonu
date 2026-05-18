"""
LogiAI Data Ingestion Module
Excel veya CSV verilerini temizleyip Redis'e yüklemek için Pandas-based pipeline.
"""

import logging
from datetime import datetime
from typing import Dict, List, Tuple

import pandas as pd
import redis

from src.utils.config import get_redis_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def _get_client() -> redis.Redis:
    """Lazy Redis bağlantısı — import sırasında bağlanmaz."""
    return get_redis_client()


def validate_transfer_centers(df: pd.DataFrame) -> Tuple[pd.DataFrame, List[str]]:
    """Transfer Merkezi verilerini doğrular ve temizler.

    Beklenen Sütunlar:
    - TM_ID: str, format='XX_XX' (ör: 34_01)
    - Capacity: int, positif sayı
    """
    errors = []

    required_cols = ['TM_ID', 'Capacity']
    missing_cols = [col for col in required_cols if col not in df.columns]
    if missing_cols:
        error_msg = f"Eksik sütunlar: {missing_cols}"
        errors.append(error_msg)
        logger.error(error_msg)
        return pd.DataFrame(), errors

    tm_format_mask = df['TM_ID'].astype(str).str.match(r'^\d{2}_\d{2}$')
    bad_ids = df[~tm_format_mask]['TM_ID'].tolist()
    if bad_ids:
        error_msg = f"Yanlis TM_ID formati (XX_XX bekleniyor): {bad_ids}"
        errors.append(error_msg)
        logger.warning(error_msg)
        df = df[tm_format_mask]

    try:
        df['Capacity'] = pd.to_numeric(df['Capacity'], errors='coerce')
    except Exception as e:
        error_msg = f"Capacity sutunu sayisal degerlere donusturulemedi: {e}"
        errors.append(error_msg)
        logger.error(error_msg)
        return pd.DataFrame(), errors

    negative_caps = df[df['Capacity'] <= 0]
    if not negative_caps.empty:
        error_msg = f"Negatif/Sifir kapasite bulundu, satirlar kaldirildi: {len(negative_caps)}"
        errors.append(error_msg)
        logger.warning(error_msg)
        df = df[df['Capacity'] > 0]

    duplicates = df[df.duplicated(subset=['TM_ID'], keep=False)]['TM_ID'].unique().tolist()
    if duplicates:
        error_msg = f"Duplikat TM_ID'ler (ilk kaydi tutuldu): {duplicates}"
        errors.append(error_msg)
        logger.warning(error_msg)
        df = df.drop_duplicates(subset=['TM_ID'], keep='first')

    df.reset_index(drop=True, inplace=True)
    logger.info(f"{len(df)} adet Transfer Merkezi dogrulanadi.")
    return df, errors


def validate_vehicles(df: pd.DataFrame) -> Tuple[pd.DataFrame, List[str]]:
    """Araç verilerini doğrular ve temizler.

    Beklenen Sütunlar:
    - Vehicle_ID: str, unique
    - Type: str (Tır, Kamyon, Hafif Kamyon, Kamyonet)
    - Capacity: int, positif sayı
    """
    errors = []

    required_cols = ['Vehicle_ID', 'Type', 'Capacity']
    missing_cols = [col for col in required_cols if col not in df.columns]
    if missing_cols:
        error_msg = f"Eksik sütunlar: {missing_cols}"
        errors.append(error_msg)
        logger.error(error_msg)
        return pd.DataFrame(), errors

    valid_types = {'Tır', 'Kamyon', 'Hafif Kamyon', 'Kamyonet'}
    invalid_types = df[~df['Type'].isin(valid_types)]['Type'].unique().tolist()
    if invalid_types:
        error_msg = f"Gecersiz arac tipleri kaldirildi: {invalid_types}"
        errors.append(error_msg)
        logger.warning(error_msg)
        df = df[df['Type'].isin(valid_types)]

    try:
        df['Capacity'] = pd.to_numeric(df['Capacity'], errors='coerce')
    except Exception as e:
        error_msg = f"Capacity sutunu sayisal degerlere donusturulemedi: {e}"
        errors.append(error_msg)
        logger.error(error_msg)
        return pd.DataFrame(), errors

    negative_caps = df[df['Capacity'] <= 0]
    if not negative_caps.empty:
        error_msg = f"Negatif/Sifir kapasite bulundu, satirlar kaldirildi: {len(negative_caps)}"
        errors.append(error_msg)
        logger.warning(error_msg)
        df = df[df['Capacity'] > 0]

    duplicates = df[df.duplicated(subset=['Vehicle_ID'], keep=False)]['Vehicle_ID'].unique().tolist()
    if duplicates:
        error_msg = f"Duplikat Vehicle_ID'ler (ilk kaydi tutuldu): {duplicates}"
        errors.append(error_msg)
        logger.warning(error_msg)
        df = df.drop_duplicates(subset=['Vehicle_ID'], keep='first')

    df.reset_index(drop=True, inplace=True)
    logger.info(f"{len(df)} adet arac dogrulanadi.")
    return df, errors


def load_transfer_centers_to_redis(df: pd.DataFrame) -> Dict:
    """Doğrulanmış TM verilerini Redis HASH'lerine yükler."""
    result = {}
    client = _get_client()

    for _, row in df.iterrows():
        tm_id = row['TM_ID']
        capacity = int(row['Capacity'])
        try:
            client.hset(f"TM:{tm_id}:State", mapping={
                "MaxCapacity":    capacity,
                "CurrentLoad":    0,
                "OverloadAmount": 0,
                "LoadedAt":       datetime.now().isoformat(),
            })
            result[tm_id] = "Basarili"
            logger.info(f"TM:{tm_id} Redis'e yuklendi (Kapasite: {capacity})")
        except Exception as e:
            result[tm_id] = f"Hata: {str(e)}"
            logger.error(f"TM:{tm_id} Redis yukleme hatasi: {e}")

    logger.info(f"{len(df)} adet TM Redis'e yuklendi.")
    return result


def load_vehicles_to_redis(df: pd.DataFrame) -> Dict:
    """Doğrulanmış araç verilerini Redis HASH'lerine yükler."""
    result = {}
    client = _get_client()

    for _, row in df.iterrows():
        v_id = row['Vehicle_ID']
        v_type = row['Type']
        capacity = int(row['Capacity'])
        try:
            client.hset(f"Vehicle:{v_id}:State", mapping={
                "Type":        v_type,
                "MaxCapacity": capacity,
                "CurrentLoad": 0,
                "Location":    "Depo",
                "LoadedAt":    datetime.now().isoformat(),
            })
            result[v_id] = "Basarili"
            logger.info(f"Vehicle:{v_id} Redis'e yuklendi (Tip: {v_type}, Kapasite: {capacity})")
        except Exception as e:
            result[v_id] = f"Hata: {str(e)}"
            logger.error(f"Vehicle:{v_id} Redis yukleme hatasi: {e}")

    logger.info(f"{len(df)} adet arac Redis'e yuklendi.")
    return result


def ingest_from_excel(file_path: str) -> Dict:
    """Excel dosyasindan TM ve arac verilerini oku, dogrula ve Redis'e yukle.

    Beklenen yapi:
    - Sheet 1: "Transfer Merkezleri" (TM_ID, Capacity)
    - Sheet 2: "Araclar" (Vehicle_ID, Type, Capacity)
    """
    logger.info(f"Excel dosyasi okuluyor: {file_path}")

    all_errors = []
    tm_results = {}
    vehicle_results = {}

    try:
        tm_df = pd.read_excel(file_path, sheet_name="Transfer Merkezleri")
        logger.info(f"{len(tm_df)} satir TM verisi okundu.")
        tm_df, tm_errors = validate_transfer_centers(tm_df)
        all_errors.extend(tm_errors)
        if not tm_df.empty:
            tm_results = load_transfer_centers_to_redis(tm_df)
        else:
            logger.warning("Gecerli TM verisi yok!")
    except Exception as e:
        error_msg = f"TM verisi isleme hatasi: {e}"
        all_errors.append(error_msg)
        logger.error(error_msg)

    try:
        vehicle_df = pd.read_excel(file_path, sheet_name="Araclar")
        logger.info(f"{len(vehicle_df)} satir arac verisi okundu.")
        vehicle_df, vehicle_errors = validate_vehicles(vehicle_df)
        all_errors.extend(vehicle_errors)
        if not vehicle_df.empty:
            vehicle_results = load_vehicles_to_redis(vehicle_df)
        else:
            logger.warning("Gecerli arac verisi yok!")
    except Exception as e:
        error_msg = f"Arac verisi isleme hatasi: {e}"
        all_errors.append(error_msg)
        logger.error(error_msg)

    status = "success" if not all_errors else ("partial" if (tm_results or vehicle_results) else "error")

    return {
        "status":                status,
        "timestamp":             datetime.now().isoformat(),
        "tm_results":            tm_results,
        "vehicle_results":       vehicle_results,
        "errors":                all_errors,
        "total_tm_loaded":       len(tm_results),
        "total_vehicles_loaded": len(vehicle_results),
    }


if __name__ == "__main__":
    logger.info("Data Ingestion modulu calistirildi.")
