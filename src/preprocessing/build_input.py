"""
build_input.py
==============
Excel ham verilerini okuyup pipeline'ın beklediği logiai_mvp_input.json'u üretir.

Girdiler (data/raw/):
  - Koordinatlar v2.xlsx     → şehir adları + koordinatlar
  - Araç_Kapasite_Maliyet.xlsx → araç tipleri, kapasite, maliyet
  - Kiralık_Araçlar.xlsx     → kiralık filo rotaları
  - Desi_talep.xlsx          → günlük OD talep matrisi

Çıktı (data/raw/):
  - logiai_mvp_input.json

Kullanım:
  python -m src.preprocessing.build_input
  python -m src.preprocessing.build_input --output data/raw/logiai_mvp_input.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
from collections import defaultdict
from pathlib import Path

import openpyxl

# ── Sabitler ─────────────────────────────────────────────────────────────────

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent.parent.parent / "data" / "raw"))

# Şehir adı → kısa ID
CITY_ID: dict[str, str] = {
    "Mersin":     "MRS",
    "Kütahya":    "KUT",
    "Kocaeli":    "KOC",
    "Eskişehir":  "ESK",
    "İstanbul":   "IST",
    "Bilecik":    "BLK",
    "Balıkesir":  "BAL",
    "Şanlıurfa":  "SAN",
    "Tekirdağ":   "TEK",
    "Sivas":      "SIV",
    "Yalova":     "YAL",
    "Manisa":     "MAN",
    "Isparta":    "ISP",
    "Mardin":     "MAR",
    "Erzincan":   "ERZ",
    "Zonguldak":  "ZNG",
    "Karaman":    "KAR",
    "Denizli":    "DEN",
}

# Araç adı → kısa tip kodu
VEHICLE_TYPE: dict[str, str] = {
    "Tır":           "TIR",
    "Kamyon":        "KAM",
    "Hafif Kamyon":  "HAF",
    "Kamyonet":      "KMT",
}


# ── Yardımcılar ───────────────────────────────────────────────────────────────

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a = math.sin(dφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(dλ / 2) ** 2
    return round(R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)), 1)


def _find_file(keyword: str) -> Path:
    """keyword içeren ilk .xlsx dosyasını döner (büyük/küçük harf duyarsız, ASCII normalize)."""
    import unicodedata
    def _norm(s: str) -> str:
        return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    kw = _norm(keyword)
    for f in DATA_DIR.iterdir():
        if f.suffix == ".xlsx" and kw in _norm(f.stem):
            return f
    raise FileNotFoundError(f"'{keyword}*.xlsx' bulunamadı: {DATA_DIR}")


def _rows(path: Path) -> list[tuple]:
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    data = list(ws.iter_rows(values_only=True))
    wb.close()
    return data


# ── Okuyucular ────────────────────────────────────────────────────────────────

def read_cities() -> dict[str, tuple[float, float]]:
    """{'IST': (lat, lon), ...}"""
    rows = _rows(_find_file("Koordinatlar"))
    cities: dict[str, tuple[float, float]] = {}
    for row in rows[1:]:
        name, lat, lon = row[0], row[1], row[2]
        if name and lat is not None and lon is not None:
            city_id = CITY_ID.get(str(name).strip())
            if city_id:
                cities[city_id] = (float(lat), float(lon))
    return cities


def read_vehicle_specs() -> dict[str, dict]:
    """
    {
      'TIR': {'capacity_desi': 22400, 'rental_daily': 7000, 'rental_km': 13,
              'spot_daily': 11700, 'spot_km': 25},
      ...
    }
    """
    rows = _rows(_find_file("Araç_Kapasite"))
    specs: dict[str, dict] = {}
    for row in rows[1:]:
        name, cap, r_day, r_km, s_day, s_km = row
        vtype = VEHICLE_TYPE.get(str(name).strip())
        if vtype:
            specs[vtype] = {
                "capacity_desi": int(cap),
                "rental_daily":  float(r_day),
                "rental_km":     float(r_km),
                "spot_daily":    float(s_day),
                "spot_km":       float(s_km),
            }
    return specs


def read_rental_routes() -> list[tuple[str, str, int, str]]:
    """[(origin_id, dest_id, count, vtype), ...]"""
    rows = _rows(_find_file("Kiralık"))
    routes = []
    for row in rows[1:]:
        src_name, dst_name, count, vname = row
        src = CITY_ID.get(str(src_name).strip())
        dst = CITY_ID.get(str(dst_name).strip())
        vtype = VEHICLE_TYPE.get(str(vname).strip())
        if src and dst and vtype:
            routes.append((src, dst, int(count), vtype))
    return routes


def read_demand() -> dict[str, dict[str, dict[str, float]]]:
    """{'2026-01-01': {'IST': {'ANK': 1234.5, ...}, ...}, ...}"""
    rows = _rows(_find_file("Desi"))
    demand: dict[str, dict[str, dict[str, float]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(float))
    )
    skipped = 0
    for row in rows[1:]:
        src_name, dst_name, tarih, desi = row
        src = CITY_ID.get(str(src_name).strip())
        dst = CITY_ID.get(str(dst_name).strip())
        if not src or not dst or desi is None:
            skipped += 1
            continue
        date_str = tarih.strftime("%Y-%m-%d") if hasattr(tarih, "strftime") else str(tarih)[:10]
        demand[date_str][src][dst] += float(desi)
    if skipped:
        print(f"  [uyarı] {skipped} satır atlandı (bilinmeyen şehir veya boş değer)")
    return {d: {o: dict(dests) for o, dests in origs.items()} for d, origs in demand.items()}


# ── Ana İnşa Fonksiyonu ───────────────────────────────────────────────────────

def build(output_path: Path | None = None) -> dict:
    print("Veri okunuyor...")

    cities      = read_cities()
    vehicle_specs = read_vehicle_specs()
    rental_raw  = read_rental_routes()
    daily_demand = read_demand()

    city_ids = sorted(cities.keys())
    print(f"  {len(city_ids)} şehir, {len(vehicle_specs)} araç tipi, "
          f"{len(rental_raw)} kiralık rota, {len(daily_demand)} tarih")

    # ── Mesafe matrisi ────────────────────────────────────────────────────────
    distance_matrix: dict[str, dict[str, float]] = {}
    for a in city_ids:
        distance_matrix[a] = {}
        for b in city_ids:
            if a != b:
                distance_matrix[a][b] = _haversine_km(*cities[a], *cities[b])

    # ── Spot kapasiteleri ─────────────────────────────────────────────────────
    spot_capacities = {vt: s["capacity_desi"] for vt, s in vehicle_specs.items()}

    # ── Maliyet matrisi ───────────────────────────────────────────────────────
    cost_matrix: dict[str, dict[str, dict[str, dict[str, float]]]] = {}
    for o in city_ids:
        cost_matrix[o] = {}
        for d in city_ids:
            if o == d:
                continue
            km = distance_matrix[o][d]
            cost_matrix[o][d] = {}
            for vt, s in vehicle_specs.items():
                cost_matrix[o][d][vt] = {
                    "kiralik": round(s["rental_daily"] + s["rental_km"] * km, 2),
                    "spot":    round(s["spot_daily"]   + s["spot_km"]   * km, 2),
                }

    # ── Kiralık rotalar ───────────────────────────────────────────────────────
    rental_routes: dict[str, list[dict]] = {}
    vehicle_counters: dict[str, int] = {}
    for src, dst, count, vtype in rental_raw:
        key = f"{src}_{dst}"
        cap = vehicle_specs[vtype]["capacity_desi"]
        vehicles = []
        for _ in range(count):
            vehicle_counters[vtype] = vehicle_counters.get(vtype, 0) + 1
            vid = f"{vtype}{vehicle_counters[vtype]:02d}"
            vehicles.append({"id": vid, "capacity_desi": cap})
        if key in rental_routes:
            rental_routes[key].extend(vehicles)
        else:
            rental_routes[key] = vehicles

    result = {
        "distance_matrix": distance_matrix,
        "cost_matrix":     cost_matrix,
        "rental_routes":   rental_routes,
        "spot_capacities": spot_capacities,
        "daily_demand":    daily_demand,
    }

    # ── Yaz ──────────────────────────────────────────────────────────────────
    out = output_path or DATA_DIR / "logiai_mvp_input.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

    dates = sorted(daily_demand.keys())
    print(f"Yazıldı: {out}")
    print(f"Tarih aralığı: {dates[0]} — {dates[-1]} ({len(dates)} gün)")
    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Excel → logiai_mvp_input.json")
    parser.add_argument("--output", "-o", default=None, help="Çıktı JSON yolu")
    args = parser.parse_args()
    build(Path(args.output) if args.output else None)
