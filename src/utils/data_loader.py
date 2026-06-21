"""
utils/data_loader.py
====================
Sonuç JSON dosyasını (logiai_mvp_input.json) okuyup veri sözleşmesine göre
**temizler** (sanitize). Black-Box: yalnızca nihai JSON'u girdi alır; Kişi A'nın
ham veri / Pandas koduna dokunmaz.

Tasarım — Graceful Degradation (Kaptan Kuralı #2):
    * Yalnızca JSON iskeletini bozan, motorun çalışmasını imkânsız kılan MAJÖR
      eksikler için `DataContractError` fırlatılır (fail-fast):
        - Dosya yok / geçersiz JSON
        - Zorunlu üst düzey anahtar eksik
        - Zorunlu üst düzey değer dict değil
        - Temizlik sonrası hiç geçerli araç tipi / şehir / talep kalmaması
    * Alt dallardaki bozuk kayıtlar (eşleşmeyen şehir, negatif değer, None fiyat,
      bozuk tarih) ÇÖKERTMEZ — atlanır (skip), şeffaf bir uyarı (warning) basılır
      ve sağlam veri işlenmeye devam eder.

SOLID — Single Responsibility: yalnızca I/O + şema temizliği. İş mantığı girmez.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Şema Sabitleri
# ─────────────────────────────────────────────────────────────────────────────

# Motorun çalışması için ZORUNLU üst düzey anahtarlar (eksikse iskelet bozuk).
# spot_capacities bilinçli olarak burada DEĞİL: yoksa vehicles_info'dan türetilir.
_REQUIRED_TOP_KEYS: frozenset[str] = frozenset({
    "distance_matrix",
    "cost_matrix",
    "rental_routes",
    "daily_demand",
    "vehicles_info",
})

# Kiralık fiyat anahtarı her iki Türkçe yazımıyla da kabul edilir.
_RENTAL_PRICE_KEYS: tuple[str, ...] = ("kiralik", "kiralık")


# ─────────────────────────────────────────────────────────────────────────────
# Özel İstisna — yalnızca İSKELET bozulduğunda fırlatılır
# ─────────────────────────────────────────────────────────────────────────────

class DataContractError(ValueError):
    """
    JSON iskeleti motorun çalışmasını imkânsız kılacak biçimde bozuk olduğunda
    fırlatılır. Alt dal bozuklukları bu hatayı tetiklemez; atlanır + uyarılır.
    """


# ─────────────────────────────────────────────────────────────────────────────
# İskelet Denetimi (hard-fail)
# ─────────────────────────────────────────────────────────────────────────────

def _require_skeleton(data: dict[str, Any]) -> None:
    """Üst düzey zorunlu anahtarların varlığını ve dict tipini denetler."""
    if not isinstance(data, dict):
        raise DataContractError("Kök JSON bir nesne (dict) olmalıdır.")

    missing = _REQUIRED_TOP_KEYS - data.keys()
    if missing:
        raise DataContractError(f"Eksik üst düzey alanlar: {sorted(missing)}")

    for key in _REQUIRED_TOP_KEYS:
        if not isinstance(data[key], dict):
            raise DataContractError(
                f"'{key}' bir dict olmalıdır (iskelet bozuk); alınan: {type(data[key]).__name__}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# Sanitizer'lar (skip + warn) — her biri TEMİZLENMİŞ yapıyı döndürür
# ─────────────────────────────────────────────────────────────────────────────

def _sanitize_vehicles_info(vehicles_info: dict) -> dict:
    clean: dict = {}
    for vtype, info in vehicles_info.items():
        if not isinstance(info, dict) or "capacity_desi" not in info:
            log.warning("vehicles_info['%s'] atlandı (geçersiz yapı).", vtype)
            continue
        cap = info["capacity_desi"]
        if not isinstance(cap, (int, float)) or cap <= 0:
            log.warning("vehicles_info['%s'] atlandı (kapasite geçersiz: %r).", vtype, cap)
            continue
        clean[vtype] = info
    if not clean:
        raise DataContractError("vehicles_info'da geçerli araç tipi yok (iskelet bozuk).")
    return clean


def _resolve_spot_capacities(data: dict, vehicles_info: dict) -> dict:
    """
    spot_capacities yoksa/boşsa vehicles_info'dan türetir (SSoT + graceful).
    Varsa geçersiz değerleri atlar; hepsi geçersizse yine türetir.
    """
    def _derive() -> dict:
        return {v: info["capacity_desi"] for v, info in vehicles_info.items()}

    raw = data.get("spot_capacities")
    if not isinstance(raw, dict) or not raw:
        derived = _derive()
        log.warning("spot_capacities eksik/boş; vehicles_info'dan türetildi (%d tip).", len(derived))
        return derived

    clean: dict = {}
    for vtype, cap in raw.items():
        if not isinstance(cap, (int, float)) or cap <= 0:
            log.warning("spot_capacities['%s'] atlandı (geçersiz: %r).", vtype, cap)
            continue
        clean[vtype] = cap
    if not clean:
        derived = _derive()
        log.warning("spot_capacities tümü geçersiz; vehicles_info'dan türetildi.")
        return derived
    return clean


def _sanitize_distance_matrix(dist_matrix: dict) -> dict:
    clean: dict = {}
    skipped = 0
    for origin, dests in dist_matrix.items():
        if not isinstance(dests, dict):
            log.warning("distance_matrix['%s'] atlandı (dict değil).", origin)
            continue
        valid = {}
        for dest, dist in dests.items():
            if isinstance(dist, (int, float)) and dist >= 0:
                valid[dest] = dist
            else:
                skipped += 1
        clean[origin] = valid
    if skipped:
        log.warning("distance_matrix: %d geçersiz mesafe atlandı.", skipped)
    if not clean:
        raise DataContractError("distance_matrix boş/geçersiz (iskelet bozuk).")
    return clean


def _sanitize_rental_routes(rental_routes: dict, known_cities: set, known_types: set) -> dict:
    clean: dict = {}
    skipped_routes = 0
    skipped_vehicles = 0
    for route_key, vehicles in rental_routes.items():
        if "_" not in route_key or not isinstance(vehicles, list):
            log.warning("rental_routes['%s'] atlandı (anahtar/liste geçersiz).", route_key)
            skipped_routes += 1
            continue
        origin, dest = route_key.split("_", 1)
        if origin not in known_cities or dest not in known_cities:
            log.warning(
                "rental_routes['%s'] atlandı (şehir distance_matrix'te yok).", route_key
            )
            skipped_routes += 1
            continue
        valid = []
        for v in vehicles:
            if not isinstance(v, dict) or "id" not in v or "capacity_desi" not in v:
                skipped_vehicles += 1
                continue
            cap = v["capacity_desi"]
            if not isinstance(cap, (int, float)) or cap <= 0:
                skipped_vehicles += 1
                continue
            vtype = v.get("vehicle_type", "")
            if vtype not in known_types:
                log.warning(
                    "rental_routes['%s'] araç '%s' atlandı (vehicle_type '%s' vehicles_info'da yok).",
                    route_key, v.get("id", "?"), vtype,
                )
                skipped_vehicles += 1
                continue
            valid.append(v)
        if valid:
            clean[route_key] = valid
        else:
            skipped_routes += 1
    if skipped_routes or skipped_vehicles:
        log.warning(
            "rental_routes: %d rota, %d araç atlandı.", skipped_routes, skipped_vehicles
        )
    return clean


def _sanitize_cost_matrix(cost_matrix: dict, known_types: set) -> dict:
    """
    Her (origin, dest, vtype) kaydında geçerli bir kiralık fiyatı VE 'spot'
    fiyatı (negatif olmayan sayı) bulunmalı; aksi halde o kayıt atlanır.
    vehicles_info'da tanımsız araç tipleri de atlanır.
    """
    def _ok(x: Any) -> bool:
        return isinstance(x, (int, float)) and x >= 0

    clean: dict = {}
    skipped = 0
    for origin, dests in cost_matrix.items():
        if not isinstance(dests, dict):
            continue
        clean_dests: dict = {}
        for dest, vehicle_costs in dests.items():
            if not isinstance(vehicle_costs, dict):
                continue
            clean_vtypes: dict = {}
            for vtype, prices in vehicle_costs.items():
                if vtype not in known_types or not isinstance(prices, dict):
                    skipped += 1
                    continue
                rental = next((prices[k] for k in _RENTAL_PRICE_KEYS if k in prices), None)
                if not _ok(rental) or not _ok(prices.get("spot")):
                    skipped += 1
                    continue
                clean_vtypes[vtype] = prices
            if clean_vtypes:
                clean_dests[dest] = clean_vtypes
        if clean_dests:
            clean[origin] = clean_dests
    if skipped:
        log.warning("cost_matrix: %d geçersiz fiyat kaydı atlandı.", skipped)
    return clean


def _sanitize_daily_demand(daily_demand: dict, known_cities: set) -> dict:
    clean: dict = {}
    skipped = 0
    for date_str, origins in daily_demand.items():
        try:
            date.fromisoformat(date_str)
        except (ValueError, TypeError):
            log.warning("daily_demand: '%s' geçersiz tarih anahtarı, atlandı.", date_str)
            continue
        if not isinstance(origins, dict):
            continue
        clean_origins: dict = {}
        for origin, dests in origins.items():
            if origin not in known_cities or not isinstance(dests, dict):
                skipped += len(dests) if isinstance(dests, dict) else 1
                continue
            valid = {}
            for dest, desi in dests.items():
                if dest not in known_cities:
                    skipped += 1
                    continue
                try:
                    desi_f = float(desi)
                except (ValueError, TypeError):
                    log.warning(
                        "daily_demand['%s']['%s']['%s'] sayıya çevrilemedi (%r), atlandı.",
                        date_str, origin, dest, desi,
                    )
                    skipped += 1
                    continue
                if desi_f >= 0:
                    valid[dest] = desi_f
                else:
                    skipped += 1
            if valid:
                clean_origins[origin] = valid
        if clean_origins:
            clean[date_str] = clean_origins
    if skipped:
        log.warning("daily_demand: %d geçersiz talep kaydı atlandı.", skipped)
    if not clean:
        raise DataContractError("daily_demand'da geçerli talep kalmadı (iskelet bozuk).")
    return clean


# ─────────────────────────────────────────────────────────────────────────────
# Herkese Açık API
# ─────────────────────────────────────────────────────────────────────────────

def load_input(json_path: str | Path) -> dict[str, Any]:
    """
    JSON dosyasını okur, iskeleti denetler, alt dalları temizler ve
    **sağlam** bir Python sözlüğü döndürür.

    Raises
    ------
    DataContractError
        Yalnızca dosya/JSON/iskelet düzeyinde majör bir bozukluk varsa.
        Alt dal bozuklukları fırlatmaz; atlanır + uyarılır.
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

    # ── İskelet (hard-fail) ──
    _require_skeleton(data)

    # ── Alt dal temizliği (skip + warn) ──
    vehicles_info = _sanitize_vehicles_info(data["vehicles_info"])
    distance_matrix = _sanitize_distance_matrix(data["distance_matrix"])
    known_cities = set(distance_matrix.keys())
    spot_capacities = _resolve_spot_capacities(data, vehicles_info)
    rental_routes = _sanitize_rental_routes(data["rental_routes"], known_cities, set(vehicles_info.keys()))
    cost_matrix = _sanitize_cost_matrix(data["cost_matrix"], set(vehicles_info.keys()))
    daily_demand = _sanitize_daily_demand(data["daily_demand"], known_cities)

    # Ekstra anahtarları (city_coords, vehicle_types, tir_yanasma…) koru
    cleaned = dict(data)
    cleaned.update({
        "vehicles_info":   vehicles_info,
        "spot_capacities": spot_capacities,
        "distance_matrix": distance_matrix,
        "rental_routes":   rental_routes,
        "cost_matrix":     cost_matrix,
        "daily_demand":    daily_demand,
    })

    dates = sorted(daily_demand.keys())
    log.info(
        "Yüklendi: %d tarih, %d şehir, %d araç tipi, %d kiralık rota.",
        len(dates), len(known_cities), len(vehicles_info), len(rental_routes),
    )
    return cleaned


def available_dates(data: dict[str, Any]) -> list[str]:
    """Veri setindeki planlama tarihlerini sıralı döndürür."""
    return sorted(data.get("daily_demand", {}).keys())
