"""
optimization/greedy.py
======================
Aşama 1 — Greedy Kiralık Araç Kapasitesi Atama

SOLID — Single Responsibility:
    Yalnızca kiralık araç ataması yapar.
    OR-Tools, spot maliyet hesabı veya I/O buraya girmez.

SOLID — Dependency Inversion:
    Dışarıdan ham dict (data contract) alır; somut bir sınıfa değil,
    sözleşmeye bağımlıdır. İleride bir LinehaulRepository arayüzüne
    dönüştürülmesi kolaylaştırılmıştır.

Algoritma:
    1. Günlük talepleri (origin, dest, desi) üçlülerine dönüştür.
    2. Büyükten küçüğe sırala (splittable demand için greedy optimum).
    3. Her talep için aynı O-D hattına ait kiralık araçlara, kalan
       kapasiteleri ölçüsünde yükle.
    4. Araç kapasitesi dolunca artan kısımı (spill) Aşama 2'ye aktar.
"""

from __future__ import annotations

import logging
from typing import Any

from models.data_types import RentalAssignment, RouteKey

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Sabitler
# ─────────────────────────────────────────────────────────────────────────────

_FLOAT_ZERO_TOLERANCE: float = 1e-6   # kayan nokta sıfır eşiği


# ─────────────────────────────────────────────────────────────────────────────
# Yardımcı Fonksiyonlar  (private)
# ─────────────────────────────────────────────────────────────────────────────

def _infer_vehicle_class(vehicle_id: str) -> str:
    """
    Araç ID'sinden maliyet matrisinde kullanılan araç sınıfını çıkarır.

    Kural tabanlı basit eşleşme; ileride bir konfigürasyon dosyasına
    taşınabilir.

    Örnekler:
        "KIR_TIR_01"      → "Tır"
        "KIR_KAM_01"      → "Kamyon"
        "KIR_HAFIF_01"    → "Hafif Kamyon"
        "KIR_KAMYONET_01" → "Kamyonet"
    """
    vid = vehicle_id.upper()
    if "TIR" in vid:
        return "Tır"
    if "KAMYONET" in vid or "KNET" in vid:
        return "Kamyonet"
    if "HAFIF" in vid:
        return "Hafif Kamyon"
    if "KAM" in vid:
        return "Kamyon"
    log.warning(
        "Araç ID'sinden sınıf çıkarılamadı: '%s'. 'Tır' varsayılıyor.",
        vehicle_id,
    )
    return "Tır"


def _build_vehicle_pool(
    rental_routes: dict[str, list[dict]],
    cost_matrix: dict,
) -> dict[str, dict]:
    """
    rental_routes verisinden her araç için mutable bir durum sözlüğü üretir.

    Dönen yapı:
        {
            vehicle_id: {
                "origin":     str,
                "dest":       str,
                "capacity":   float,
                "remaining":  float,   ← bu değer atama sırasında güncellenir
                "assigned":   float,
                "unit_cost":  float,
            }
        }
    """
    pool: dict[str, dict] = {}

    for route_key, vehicles in rental_routes.items():
        # Anahtar biçimi: "ŞehirA_ŞehirB"
        # _validate_schema zaten "_" içerdiğini garantiledi; yine de savunmacı
        if "_" not in route_key:
            log.warning("Geçersiz rental_routes anahtarı atlandı: '%s'", route_key)
            continue

        origin, dest = route_key.split("_", 1)

        for vehicle in vehicles:
            vid = vehicle["id"]
            cap = float(vehicle["capacity_desi"])
            vclass = _infer_vehicle_class(vid)

            try:
                unit_cost = float(cost_matrix[origin][dest][vclass]["kiralik"])
            except (KeyError, TypeError):
                log.warning(
                    "Kiralık maliyet bulunamadı: cost_matrix['%s']['%s']['%s']"
                    "['kiralik']. Maliyet 0.0 olarak atandı.",
                    origin, dest, vclass,
                )
                unit_cost = 0.0

            pool[vid] = {
                "origin":    origin,
                "dest":      dest,
                "capacity":  cap,
                "remaining": cap,
                "assigned":  0.0,
                "unit_cost": unit_cost,
            }

    log.debug("%d kiralık araç havuza eklendi.", len(pool))
    return pool


def _flatten_and_sort_demand(
    daily_demand: dict,
    date: str,
) -> list[tuple[str, str, float]]:
    """
    Günlük talep iç içe sözlüğünü (origin, dest, desi) üçlü listesine
    dönüştürür; aynı O-D çiftinden gelen talepleri **önceden toplar**,
    ardından büyükten küçüğe sıralar.

    Ön-toplama (pre-aggregation) neden gerekli:
        Veri kaynağı aynı O-D hattında birden fazla sipariş satırı
        içerebilir. Bunlar ayrı ayrı sıralanırsa küçük partiler büyük
        bir talebin önüne geçerek araç kapasitesini verimsiz doldurur.
        Toplanarak tek satır haline getirildiklerinde greedy sıralaması
        doğru önceliği verir.
    """
    # Adım 1: aynı O-D çiftini topla
    aggregated: dict[tuple[str, str], float] = {}
    for origin, destinations in daily_demand.get(date, {}).items():
        for dest, desi in destinations.items():
            desi_f = float(desi)
            if desi_f > _FLOAT_ZERO_TOLERANCE:
                key = (origin, dest)
                aggregated[key] = aggregated.get(key, 0.0) + desi_f

    # Adım 2: dict → liste, büyükten küçüğe sırala
    items: list[tuple[str, str, float]] = [
        (o, d, desi) for (o, d), desi in aggregated.items()
    ]
    items.sort(key=lambda x: x[2], reverse=True)
    return items


# ─────────────────────────────────────────────────────────────────────────────
# Herkese Açık API
# ─────────────────────────────────────────────────────────────────────────────

def run_greedy_assignment(
    data: dict[str, Any],
    date: str,
) -> tuple[list[RentalAssignment], dict[RouteKey, float]]:
    """
    Kiralık araçlara greedy kapasite ataması yapar.

    Büyük talepten başlayarak her O-D hattındaki kiralık araçlara kargo
    atar. Araç kapasitesi dolduğunda kalan talep (spill) ikinci aşama
    için döndürülür.

    Parameters
    ----------
    data : dict
        Veri sözleşmesine uygun Python sözlüğü.
    date : str
        İşlenecek gün (ISO 8601 biçimi, örn. "2026-05-23").

    Returns
    -------
    tuple
        - list[RentalAssignment] : gerçekleşen atamalar
        - dict[RouteKey, float]  : spill talepleri {(origin, dest): desi}
    """
    cost_matrix   = data["cost_matrix"]
    rental_routes = data["rental_routes"]
    daily_demand  = data["daily_demand"]

    demand_items = _flatten_and_sort_demand(daily_demand, date)

    if not demand_items:
        log.warning("'%s' için talep bulunamadı; Aşama 1 atlandı.", date)
        return [], {}

    vehicle_pool = _build_vehicle_pool(rental_routes, cost_matrix)

    rental_assignments: list[RentalAssignment] = []
    spill_demand: dict[RouteKey, float] = {}

    for origin, dest, total_desi in demand_items:
        remaining = total_desi

        # Bu O-D çiftine ait, kapasitesi olan araçları bul
        matching = [
            (vid, info)
            for vid, info in vehicle_pool.items()
            if info["origin"] == origin
            and info["dest"]   == dest
            and info["remaining"] > _FLOAT_ZERO_TOLERANCE
        ]

        # Kalan kapasitesi en çok olan araç önce dolar (bin-packing heuristiği)
        matching.sort(key=lambda x: x[1]["remaining"], reverse=True)

        for vid, info in matching:
            if remaining <= _FLOAT_ZERO_TOLERANCE:
                break

            can_assign = min(remaining, info["remaining"])
            info["remaining"] -= can_assign
            info["assigned"]  += can_assign
            remaining         -= can_assign

            rental_assignments.append(
                RentalAssignment(
                    vehicle_id=vid,
                    origin=origin,
                    destination=dest,
                    assigned_desi=can_assign,
                    capacity_desi=info["capacity"],
                    cost=info["unit_cost"],
                )
            )
            log.debug(
                "Kiralık atama  %s → %s  |  araç: %s  |  %.1f desi  "
                "(kalan: %.1f / %.1f)",
                origin, dest, vid,
                can_assign, info["remaining"], info["capacity"],
            )

        # Artan kısmı spill sözlüğüne ekle
        if remaining > _FLOAT_ZERO_TOLERANCE:
            key: RouteKey = (origin, dest)
            spill_demand[key] = spill_demand.get(key, 0.0) + remaining
            log.info(
                "Spill  %s → %s  |  %.1f desi spot'a aktarıldı.",
                origin, dest, remaining,
            )

    log.info(
        "Aşama 1 tamamlandı: %d kiralık atama, %d güzergâhta spill.",
        len(rental_assignments), len(spill_demand),
    )
    return rental_assignments, spill_demand
