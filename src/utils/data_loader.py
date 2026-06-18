"""
utils/data_loader.py
====================
JSON dosyasını okuyup veri sözleşmesine (data contract) uygunluğunu doğrular.

SOLID — Single Responsibility:
    Yalnızca I/O ve şema doğrulaması yapar.
    İş mantığı veya optimizasyon kodu buraya girmez.

SOLID — Open/Closed:
    Yeni şema alanları eklendiğinde _REQUIRED_TOP_KEYS listesi ve
    _validate_schema() genişletilir; mevcut çağrı arayüzü değişmez.

Kullanım:
    from utils.data_loader import load_input

    data = load_input("logiai_mvp_input.json")
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Şema Sabitleri
# ─────────────────────────────────────────────────────────────────────────────

_REQUIRED_TOP_KEYS: frozenset[str] = frozenset({
    "distance_matrix",
    "cost_matrix",
    "rental_routes",
    "daily_demand",
    "vehicles_info",
})

_REQUIRED_COST_PRICE_TYPES: frozenset[str] = frozenset({"kiralik", "spot"})


# ─────────────────────────────────────────────────────────────────────────────
# Özel İstisna
# ─────────────────────────────────────────────────────────────────────────────

class DataContractError(ValueError):
    """
    Yüklenen JSON veri sözleşmesini ihlal ettiğinde fırlatılır.

    Mesaj, hangi alan/yolun hatalı olduğunu açıkça belirtir; bu sayede
    hata ayıklama süresi minimize edilir.
    """


# ─────────────────────────────────────────────────────────────────────────────
# Doğrulama Yardımcıları  (private)
# ─────────────────────────────────────────────────────────────────────────────

def _validate_top_level(data: dict[str, Any]) -> None:
    """Üst düzey zorunlu anahtarların varlığını denetler."""
    missing = _REQUIRED_TOP_KEYS - data.keys()
    if missing:
        raise DataContractError(
            f"Eksik üst düzey alanlar: {sorted(missing)}"
        )


def _validate_distance_matrix(dist_matrix: Any) -> None:
    """
    distance_matrix yapısını doğrular:
        { origin: { dest: float } }

    Tüm mesafe değerlerinin negatif olmayan sayı olduğunu kontrol eder.
    """
    if not isinstance(dist_matrix, dict):
        raise DataContractError("distance_matrix bir dict olmalıdır.")

    for origin, dests in dist_matrix.items():
        if not isinstance(dests, dict):
            raise DataContractError(
                f"distance_matrix['{origin}'] bir dict olmalıdır."
            )
        for dest, dist in dests.items():
            if not isinstance(dist, (int, float)) or dist < 0:
                raise DataContractError(
                    f"distance_matrix['{origin}']['{dest}'] negatif olmayan sayı olmalıdır; "
                    f"alınan: {dist!r}"
                )



def _validate_rental_routes(rental_routes: Any) -> None:
    """
    rental_routes:
        { "Şehir1_Şehir2": [{"id": str, "capacity_desi": number}, ...] }
    """
    if not isinstance(rental_routes, dict):
        raise DataContractError("rental_routes bir dict olmalıdır.")

    for route_key, vehicles in rental_routes.items():
        if "_" not in route_key:
            raise DataContractError(
                f"rental_routes anahtarı 'Kaynak_Hedef' biçiminde olmalı; "
                f"alınan: '{route_key}'"
            )
        if not isinstance(vehicles, list):
            raise DataContractError(
                f"rental_routes['{route_key}'] bir liste olmalıdır."
            )
        for idx, v in enumerate(vehicles):
            for field in ("id", "capacity_desi"):
                if field not in v:
                    raise DataContractError(
                        f"rental_routes['{route_key}'][{idx}] "
                        f"'{field}' alanını içermiyor."
                    )
            cap = v["capacity_desi"]
            if not isinstance(cap, (int, float)) or cap <= 0:
                raise DataContractError(
                    f"rental_routes['{route_key}'][{idx}]['capacity_desi'] "
                    f"pozitif sayı olmalıdır; alınan: {cap!r}"
                )


def _validate_cost_matrix(cost_matrix: Any, vehicles_info: dict) -> None:
    """
    cost_matrix yapısını doğrular:
        { origin: { dest: { vehicle_type: { "kiralik": n, "spot": n } } } }

    Her (origin, dest, vehicle_type) kaydında hem "kiralik" hem "spot"
    anahtarının bulunması ve değerlerinin negatif olmayan sayı olması beklenir.
    """
    if not isinstance(cost_matrix, dict):
        raise DataContractError("cost_matrix bir dict olmalıdır.")

    known_types = set(vehicles_info.keys())

    for origin, destinations in cost_matrix.items():
        if not isinstance(destinations, dict):
            raise DataContractError(
                f"cost_matrix['{origin}'] bir dict olmalıdır."
            )
        for dest, vehicle_costs in destinations.items():
            if not isinstance(vehicle_costs, dict):
                raise DataContractError(
                    f"cost_matrix['{origin}']['{dest}'] bir dict olmalıdır."
                )
            for vtype, prices in vehicle_costs.items():
                if vtype not in known_types:
                    log.warning(
                        "cost_matrix['%s']['%s']['%s'] vehicles_info'da "
                        "tanımlı değil; atlanıyor.",
                        origin, dest, vtype,
                    )
                    continue
                if not isinstance(prices, dict):
                    raise DataContractError(
                        f"cost_matrix['{origin}']['{dest}']['{vtype}'] bir dict olmalıdır."
                    )
                missing_price_keys = _REQUIRED_COST_PRICE_TYPES - prices.keys()
                if missing_price_keys:
                    raise DataContractError(
                        f"cost_matrix['{origin}']['{dest}']['{vtype}'] "
                        f"eksik fiyat anahtarları: {sorted(missing_price_keys)}"
                    )
                for price_key in _REQUIRED_COST_PRICE_TYPES:
                    val = prices.get(price_key)
                    if val is not None and (not isinstance(val, (int, float)) or val < 0):
                        raise DataContractError(
                            f"cost_matrix['{origin}']['{dest}']['{vtype}']['{price_key}'] "
                            f"negatif olmayan sayı olmalıdır; alınan: {val!r}"
                        )


def _validate_daily_demand(daily_demand: Any) -> None:
    """
    daily_demand yapısını doğrular:
        { "YYYY-MM-DD": { origin: { dest: float } } }
    """
    if not isinstance(daily_demand, dict):
        raise DataContractError("daily_demand bir dict olmalıdır.")

    for date_str, origins in daily_demand.items():
        if not isinstance(origins, dict):
            raise DataContractError(
                f"daily_demand['{date_str}'] bir dict olmalıdır."
            )
        for origin, destinations in origins.items():
            if not isinstance(destinations, dict):
                raise DataContractError(
                    f"daily_demand['{date_str}']['{origin}'] "
                    f"bir dict olmalıdır."
                )
            for dest, desi in destinations.items():
                if not isinstance(desi, (int, float)) or desi < 0:
                    raise DataContractError(
                        f"daily_demand['{date_str}']['{origin}']"
                        f"['{dest}'] negatif olamaz; alınan: {desi!r}"
                    )


def _validate_vehicles_info(vehicles_info: Any) -> None:
    """
    vehicles_info yapısını doğrular:
        { vehicle_type: { "name": str, "capacity_desi": number, ... } }
    """
    if not isinstance(vehicles_info, dict):
        raise DataContractError("vehicles_info bir dict olmalıdır.")
    for vtype, info in vehicles_info.items():
        if not isinstance(info, dict):
            raise DataContractError(
                f"vehicles_info['{vtype}'] bir dict olmalıdır."
            )
        if "capacity_desi" not in info:
            raise DataContractError(
                f"vehicles_info['{vtype}'] 'capacity_desi' alanını içermiyor."
            )
        cap = info["capacity_desi"]
        if not isinstance(cap, (int, float)) or cap <= 0:
            raise DataContractError(
                f"vehicles_info['{vtype}']['capacity_desi'] pozitif sayı olmalıdır; "
                f"alınan: {cap!r}"
            )


def _validate_demand_coherence(daily_demand: dict, distance_matrix: dict) -> None:
    """daily_demand'daki şehirlerin distance_matrix'te var olduğunu doğrular."""
    known_cities = set(distance_matrix.keys())
    for date_str, origins in daily_demand.items():
        for origin, destinations in origins.items():
            if origin not in known_cities:
                raise DataContractError(
                    f"daily_demand['{date_str}'] kaynağı '{origin}' "
                    f"distance_matrix'te tanımlı değil."
                )
            for dest in destinations:
                if dest not in known_cities:
                    raise DataContractError(
                        f"daily_demand['{date_str}']['{origin}'] hedefi '{dest}' "
                        f"distance_matrix'te tanımlı değil."
                    )


def _validate_schema(data: dict[str, Any]) -> None:
    """Tüm doğrulama adımlarını sırayla çalıştırır."""
    _validate_top_level(data)
    _validate_vehicles_info(data["vehicles_info"])
    _validate_distance_matrix(data["distance_matrix"])
    _validate_rental_routes(data["rental_routes"])
    _validate_cost_matrix(data["cost_matrix"], data["vehicles_info"])
    _validate_daily_demand(data["daily_demand"])
    _validate_demand_coherence(data["daily_demand"], data["distance_matrix"])


# ─────────────────────────────────────────────────────────────────────────────
# Herkese Açık API
# ─────────────────────────────────────────────────────────────────────────────

def load_input(json_path: str | Path) -> dict[str, Any]:
    """
    JSON dosyasını okur, şema doğrulamasından geçirir ve Python dict'i döner.

    Parameters
    ----------
    json_path : str | Path
        Girdi JSON dosyasının yolu.

    Returns
    -------
    dict[str, Any]
        Veri sözleşmesine uygun Python sözlüğü.

    Raises
    ------
    DataContractError
        Dosya bulunamazsa, geçerli JSON değilse veya şema ihlali varsa.
    """
    path = Path(json_path)

    try:
        if not path.exists():
            raise FileNotFoundError(f"Girdi dosyası bulunamadı: '{path.resolve()}'")
        log.info("Girdi dosyası okunuyor: %s", path.resolve())
        with path.open(encoding="utf-8") as fh:
            data: dict[str, Any] = json.load(fh)
    except FileNotFoundError as exc:
        raise DataContractError(str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise DataContractError(
            f"JSON ayrıştırma hatası [{path.name}]: {exc.msg} (satır {exc.lineno})"
        ) from exc

    log.info("JSON ayrıştırıldı. Şema doğrulaması başlıyor…")
    _validate_schema(data)
    log.info("Şema doğrulaması başarılı.")

    dates = sorted(data["daily_demand"].keys())
    log.info(
        "Yüklenen tarihler (%d adet): %s",
        len(dates),
        ", ".join(dates) if len(dates) <= 5 else f"{dates[:3]} … {dates[-1]}",
    )

    return data


def available_dates(data: dict[str, Any]) -> list[str]:
    """Veri setindeki planlama tarihlerini sıralı döndürür."""
    return sorted(data.get("daily_demand", {}).keys())
