"""
FastAPI Gateway — Lojistik Optimizasyon Sistemi
Person D: Sistem Mimarı ve Arayüz Geliştiricisi

Modüller:
- /api/optimize        → OR-Tools rota optimizasyonu (SYNC — deprecated, BackgroundTasks kullan)
- /api/optimize/async  → Arka plan async optimizasyon (tercih edilen)
- /api/predict         → LSTM talep tahmini (placeholder)
- /api/fleet           → Filo atama durumu
- /api/tm-status       → Transfer merkezi kapasite izleme
- /api/excel           → Excel çıktı üretimi
- /api/demand          → Sayfalı talep verisi
"""

from __future__ import annotations

import datetime
import json
import logging
import math
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware  # FIX #38
from fastapi.responses import ORJSONResponse  # FIX #37
from pydantic import BaseModel, Field, field_validator
from pydantic import ConfigDict

logger = logging.getLogger(__name__)

# Proje kök dizinini sys.path'e ekle (container içinde src/ doğrudan erişilebilir)
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from main import run_pipeline, result_to_dict
from utils.data_loader import load_input as _load_input_raw, available_dates, DataContractError
from app.job_manager import create_job, set_running, set_completed, set_failed, get_job as _get_job, get_job_for_date as _get_job_for_date

# FIX #10 — ThreadPool + Semaphore
_MAX_CONCURRENT_JOBS = 4
_executor = ThreadPoolExecutor(max_workers=_MAX_CONCURRENT_JOBS)
_semaphore = threading.Semaphore(_MAX_CONCURRENT_JOBS)

app = FastAPI(
    title="Lojistik Optimizasyon API",
    description="Teknofest LogiAI — Karar Destek Sistemi Backend",
    version="1.0.0",
    default_response_class=ORJSONResponse,  # FIX #37
)

# FIX #38 — GZip sıkıştırma
app.add_middleware(GZipMiddleware, minimum_size=1000)

# FIX #5 — CORS allow_origins env var'dan (production'da kısıtlı)
_allowed_origins = os.environ.get(
    "ALLOWED_ORIGINS", "http://localhost:8501"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Proje kök dizini: src/app/main.py → ../../ (proje kökü)
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(_PROJECT_ROOT, "data", "raw"))
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", os.path.join(_PROJECT_ROOT, "data", "processed"))
INPUT_JSON = os.environ.get("INPUT_JSON", os.path.join(DATA_DIR, "logiai_mvp_input.json"))

# ──────────────────────────────────────────────
#  FIX #9 & #36 — load_input uygulama-seviye lru_cache
# ──────────────────────────────────────────────

@lru_cache(maxsize=1)
def load_input(path: str) -> dict:
    """JSON'u bir kez diskten okur; sonraki çağrılarda cache'ten döner."""
    return _load_input_raw(path)


# ──────────────────────────────────────────────
#  FIX #24 — NaN / Infinity sanitizasyon
# ──────────────────────────────────────────────

def _sanitize(obj):
    """JSON standardını ihlal eden float değerleri (NaN, Inf) None'a çevirir."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(i) for i in obj]
    return obj


# ──────────────────────────────────────────────
#  Modeller (Pydantic) — FIX #42 strict=True
# ──────────────────────────────────────────────

class OptimizeRequest(BaseModel):
    model_config = ConfigDict(strict=True)  # FIX #42

    tarih: str = Field(..., description="Planlama tarihi (YYYY-MM-DD)")
    time_limit: int = Field(540, ge=1, description="OR-Tools zaman sınırı (saniye)")

    # FIX #20 — tarih formatı doğrulaması
    @field_validator("tarih")
    @classmethod
    def validate_tarih(cls, v: str) -> str:
        try:
            datetime.date.fromisoformat(v)
        except ValueError:
            raise ValueError("tarih YYYY-MM-DD formatında olmalı (örn: 2026-01-15)")
        return v


class RentalAssignmentResponse(BaseModel):
    model_config = ConfigDict(strict=False)

    vehicle_id: str
    origin: str
    destination: str
    assigned_desi: float
    capacity_desi: float
    utilisation: float
    cost: float
    cost_type: str


class SpotAssignmentResponse(BaseModel):
    model_config = ConfigDict(strict=False)

    vehicle_type: str
    origin: str
    destination: str
    assigned_desi: float
    capacity_desi: float
    utilisation: float
    cost: float
    route_path: list[str]
    source: str


class OptimizeResponse(BaseModel):
    model_config = ConfigDict(strict=False)

    date: str
    solver_status: str
    total_rental_cost: float
    total_spot_cost: float
    total_cost: float
    fallback_count: int
    unassigned_demand: dict
    rental_assignments: list[RentalAssignmentResponse]
    spot_assignments: list[SpotAssignmentResponse]
    calisma_suresi_sn: float


class TMDurum(BaseModel):
    model_config = ConfigDict(strict=False)

    tm_id: str
    tm_ad: str
    kapasite: int
    yuk: int
    asim: int
    asim_maliyet: float


class FleetVehicle(BaseModel):
    model_config = ConfigDict(strict=False)

    arac_id: str
    tip: str
    sabit_gunluk: float
    aktif: bool
    rota: Optional[str] = None
    doluluk_pct: float = 0.0


# ──────────────────────────────────────────────
#  Yardımcı Fonksiyonlar
# ──────────────────────────────────────────────

# FIX #4 — _COORDS_XLSX bağımlılığı kaldırıldı
# Koordinatlar artık logiai_mvp_input.json["city_coords"] anahtarından okunur.
# Format: {"İstanbul": {"lat": 41.01, "lon": 28.97}, ...}

@lru_cache(maxsize=1)
def _load_city_coords() -> dict[str, dict]:
    """
    city_coords'u logiai_mvp_input.json'dan okur.
    JSON'da "city_coords" anahtarı yoksa boş dict döner (koordinatsız şehirler
    haritada varsayılan konuma yerleşir).
    """
    try:
        data = load_input(INPUT_JSON)
        return data.get("city_coords", {})
    except Exception as exc:
        logger.warning("Koordinat verisi okunamadı: %s", exc)
        return {}


# FIX #1 — int(float(desi)) → float round(2)
# FIX #21 — dict index: tarih → list[row] ile O(1) erişim

@lru_cache(maxsize=1)
def _build_demand_index() -> dict[str, list[dict]]:
    """
    daily_demand'ı {tarih: [row, ...]} formatında indeksler.
    Disk I/O bir kez yapılır (lru_cache), filtreler O(1) dict lookup.
    """
    data = load_input(INPUT_JSON)
    daily_demand = data.get("daily_demand", {})
    dates_sorted = sorted(daily_demand.keys())
    index: dict[str, list[dict]] = {}
    for day_idx, tarih in enumerate(dates_sorted, 1):
        rows = []
        for origin, dests in daily_demand[tarih].items():
            for dest, desi in dests.items():
                val = round(float(desi), 2)  # FIX #1
                if val > 0:
                    rows.append({
                        "tarih": tarih,
                        "gonderen_id": origin,
                        "alan_id": dest,
                        "talep_desi": str(val),  # FIX #1 — ondalık korundu
                        "gun": str(day_idx),
                    })
        index[tarih] = rows
    return index


def load_demand() -> list[dict]:
    """Tüm talep satırlarını düz liste olarak döner."""
    idx = _build_demand_index()
    result = []
    for rows in idx.values():
        result.extend(rows)
    return result


def load_distance_matrix() -> dict:
    """logiai_mvp_input.json'dan mesafe matrisini döner."""
    return load_input(INPUT_JSON).get("distance_matrix", {})


def load_travel_time_matrix() -> dict:
    """Seyahat süresi matrisini mesafeden türetir (ort. 80 km/saat)."""
    dist = load_distance_matrix()
    return {
        origin: {dest: round(km / 80.0, 2) for dest, km in dests.items()}
        for origin, dests in dist.items()
    }


def get_demand_for_date(date_str: str) -> list[dict]:
    """FIX #21 — O(1) dict lookup, O(N) tarama yok."""
    return _build_demand_index().get(date_str, [])


def _run_pipeline(data: dict, date: str, time_limit_sec: int = 540) -> dict:
    """
    İki aşamalı optimizasyon boru hattını çalıştırır ve JSON-uyumlu dict döner.

    Aşama 1 → Greedy kiralık atama   (optimization.greedy)
    Aşama 2 → OR-Tools Open VRP      (optimization.vrp_solver)
    """
    res = run_pipeline(data, date, time_limit_sec=time_limit_sec)
    raw = result_to_dict(res)
    return _sanitize(raw)  # FIX #24


# ──────────────────────────────────────────────
#  Endpoints
# ──────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "sistem": "Lojistik Optimizasyon API",
        "versiyon": "1.0.0",
        "durum": "aktif",
        "zaman": datetime.datetime.now(datetime.timezone.utc).isoformat(),  # FIX #31
    }


@app.get("/api/health")
def health():
    return {
        "status": "healthy",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),  # FIX #31
    }


# FIX #3 — Senkron endpoint deprecated; async kullanımı öneriliyor
@app.post("/api/optimize", response_model=OptimizeResponse, deprecated=True)
def optimize(req: OptimizeRequest):
    """
    ⚠️ DEPRECATED — Bu endpoint 9 dakikaya kadar bloke olabilir.
    Lütfen /api/optimize/async endpoint'ini kullanın.

    İki Aşamalı Optimizasyon Motoru (Ön Rapor Uyumlu)

    Aşama 1 — Greedy Kiralık Filo Atama
    Aşama 2 — OR-Tools Spot VRP
    """
    start = datetime.datetime.now(datetime.timezone.utc)  # FIX #31

    if not os.path.exists(INPUT_JSON):
        raise HTTPException(404, f"Girdi dosyası bulunamadı: {INPUT_JSON}")

    try:
        data = load_input(INPUT_JSON)
    except DataContractError as e:
        raise HTTPException(400, f"Veri sözleşmesi hatası: {e}")

    dates = available_dates(data)
    if req.tarih not in dates:
        raise HTTPException(
            404,
            f"{req.tarih} tarihi için talep verisi bulunamadı. "
            f"Mevcut tarihler: {dates}"
        )

    result = _run_pipeline(data, req.tarih, time_limit_sec=req.time_limit)

    elapsed = (datetime.datetime.now(datetime.timezone.utc) - start).total_seconds()
    result["calisma_suresi_sn"] = round(elapsed, 3)

    return result


@app.get("/api/predict")
def predict(
    tarih: str = Query(..., description="Tahmin tarihi (YYYY-MM-DD)"),
    sehir: Optional[str] = Query(None, description="Şehir filtresi"),
):
    """
    LSTM Talep Tahmini (Placeholder)
    Gelişmiş aşamada gerçek LSTM modeli entegre edilecek.
    Şimdilik historical data üzerinden basit istatistiksel tahmin.
    """
    demand_data = load_demand()

    from collections import defaultdict
    city_demand: dict[str, list[int]] = defaultdict(list)
    for r in demand_data:
        key = r["gonderen_id"] if sehir is None or r["gonderen_id"] == sehir else None
        if key:
            city_demand[key].append(int(float(r["talep_desi"])))

    predictions = []
    for city, demands in city_demand.items():
        avg = sum(demands) / len(demands) if demands else 0
        std = (sum((d - avg) ** 2 for d in demands) / len(demands)) ** 0.5 if demands else 0
        predictions.append({
            "sehir": city,
            "tahmin_tarih": tarih,
            "p50": round(avg, 0),
            "p10": round(max(0, avg - 1.28 * std), 0),
            "p90": round(avg + 1.28 * std, 0),
            "model": "statistical_baseline",
            "not": "LSTM modeli gelişmiş aşamada entegre edilecektir",
        })

    return {"tarih": tarih, "tahminler": predictions}


@app.get("/api/fleet", response_model=list[FleetVehicle])
def fleet_status(tarih: Optional[str] = Query(None)):
    """Kiralık filo durum raporu."""
    data = load_input(INPUT_JSON)

    utilisation_map: dict[str, float] = {}
    if tarih:
        job = _get_job_for_date(tarih)
        if job and job.get("status") == "COMPLETED":
            for a in job["result"].get("rental_assignments", []):
                vid = a["vehicle_id"]
                utilisation_map[vid] = min(
                    utilisation_map.get(vid, 0.0) + a["utilisation"], 1.0
                )

    result = []
    for route_key, vehicles in data.get("rental_routes", {}).items():
        origin, dest = route_key.split("_", 1)
        for v in vehicles:
            vid = v["id"]
            vtype = v.get("vehicle_type", "Tır")
            cost_row = data.get("cost_matrix", {}).get(origin, {}).get(dest, {}).get(vtype, {})
            sabit = float(cost_row.get("kiralik", cost_row.get("kiralık", 0)))
            result.append(FleetVehicle(
                arac_id=vid,
                tip=vtype,
                sabit_gunluk=sabit,
                aktif=True,
                rota=f"{origin}→{dest}",
                doluluk_pct=round(utilisation_map.get(vid, 0.0) * 100, 1),
            ))
    return result


@app.get("/api/tm-status", response_model=list[TMDurum])
def tm_status(tarih: Optional[str] = Query(None)):
    """Transfer merkezi kapasite izleme — logiai_mvp_input.json'dan türetilir."""
    data = load_input(INPUT_JSON)
    daily_demand = data.get("daily_demand", {})

    # FIX #31 — UTC-aware datetime
    target_date = tarih or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    city_max_flow: dict[str, float] = {}
    for date, origins in daily_demand.items():
        day_flow: dict[str, float] = {}
        for origin, dests in origins.items():
            for dest, desi in dests.items():
                day_flow[origin] = day_flow.get(origin, 0.0) + float(desi)
                day_flow[dest]   = day_flow.get(dest,   0.0) + float(desi)
        for city, flow in day_flow.items():
            if flow > city_max_flow.get(city, 0.0):
                city_max_flow[city] = flow

    date_flow: dict[str, float] = {}
    for origin, dests in daily_demand.get(target_date, {}).items():
        for dest, desi in dests.items():
            date_flow[origin] = date_flow.get(origin, 0.0) + float(desi)
            date_flow[dest]   = date_flow.get(dest,   0.0) + float(desi)

    result = []
    for city in sorted(city_max_flow.keys()):
        kapasite = int(city_max_flow[city] * 1.5)
        yuk      = int(date_flow.get(city, 0.0))
        asim     = max(0, yuk - kapasite)
        result.append(TMDurum(
            tm_id=city[:4].upper()
                .replace("İ", "I").replace("Ş", "S").replace("Ç", "C")
                .replace("Ğ", "G").replace("Ü", "U").replace("Ö", "O"),
            tm_ad=city,
            kapasite=kapasite,
            yuk=yuk,
            asim=asim,
            asim_maliyet=round(asim * 8.0, 2),
        ))
    return result


@app.get("/api/excel")
def generate_excel(tarih: str = Query(...)):
    """Optimizasyon sonuçlarını Excel olarak oluştur"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from fastapi.responses import FileResponse

    date_demands = get_demand_for_date(tarih)  # FIX #21 — O(1)

    if not date_demands:
        raise HTTPException(404, f"{tarih} için veri yok")

    wb = Workbook()

    # ── Sayfa 1: Rota Planı ──
    ws1 = wb.active
    ws1.title = "Rota Planı"
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")

    headers1 = ["Araç ID", "Tip", "Rota", "Kaynak", "Hedef", "Yük (desi)", "Mesafe (km)", "Süre (saat)", "Maliyet (₺)"]
    for col, h in enumerate(headers1, 1):
        cell = ws1.cell(row=1, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    mvp_data    = load_input(INPUT_JSON)
    dist_matrix = load_distance_matrix()
    time_matrix = load_travel_time_matrix()
    row_idx = 2
    total_fixed = 0
    for route_key, vehicles in mvp_data.get("rental_routes", {}).items():
        src, dst = route_key.split("_", 1)
        demand_val = next(
            (int(float(r["talep_desi"])) for r in date_demands
             if r["gonderen_id"] == src and r["alan_id"] == dst), 0
        )
        for v in vehicles:
            vtype = v.get("vehicle_type", "Tır")
            cap   = float(v.get("capacity_desi", 0))
            cost_row = mvp_data.get("cost_matrix", {}).get(src, {}).get(dst, {}).get(vtype, {})
            sabit = float(cost_row.get("kiralik", cost_row.get("kiralık", 0)))
            total_fixed += sabit
            ws1.cell(row=row_idx, column=1, value=v["id"])
            ws1.cell(row=row_idx, column=2, value="Kiralık " + vtype)
            ws1.cell(row=row_idx, column=3, value=f"{src}-{dst}")
            ws1.cell(row=row_idx, column=4, value=src)
            ws1.cell(row=row_idx, column=5, value=dst)
            ws1.cell(row=row_idx, column=6, value=min(demand_val, cap))
            ws1.cell(row=row_idx, column=7, value=dist_matrix.get(src, {}).get(dst, 0))
            ws1.cell(row=row_idx, column=8, value=time_matrix.get(src, {}).get(dst, 0))
            ws1.cell(row=row_idx, column=9, value=sabit)
            row_idx += 1

    # ── Sayfa 2: Talep Özeti ──
    ws2 = wb.create_sheet("Talep Özeti")
    headers2 = ["Tarih", "Gönderen", "Alan", "Talep (desi)"]
    for col, h in enumerate(headers2, 1):
        cell = ws2.cell(row=1, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill

    for i, r in enumerate(date_demands, 2):
        ws2.cell(row=i, column=1, value=r["tarih"])
        ws2.cell(row=i, column=2, value=r["gonderen_id"])
        ws2.cell(row=i, column=3, value=r["alan_id"])
        ws2.cell(row=i, column=4, value=float(r["talep_desi"]))  # FIX #1

    # ── Sayfa 3: Maliyet Analizi ──
    ws3 = wb.create_sheet("Maliyet Analizi")
    headers3 = ["Kalem", "Tutar (₺)"]
    for col, h in enumerate(headers3, 1):
        cell = ws3.cell(row=1, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill

    ws3.cell(row=2, column=1, value="Kiralık Filo Sabit")
    ws3.cell(row=2, column=2, value=total_fixed)
    ws3.cell(row=3, column=1, value="Spot Araç Değişken")
    ws3.cell(row=3, column=2, value="— (optimizasyon sonrası)")
    ws3.cell(row=4, column=1, value="TOPLAM")
    ws3.cell(row=4, column=2, value=total_fixed)
    ws3.cell(row=4, column=1).font = Font(bold=True)

    output_path = os.path.join(OUTPUT_DIR, f"rapor_{tarih}.xlsx")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    wb.save(output_path)

    return FileResponse(
        output_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"rapor_{tarih}.xlsx",
    )


@app.get("/api/cities")
def list_cities():
    """Tüm şehir ve TM bilgisi — koordinatlar logiai_mvp_input.json'dan okunur."""
    # FIX #4 — Excel bağımlılığı kaldırıldı
    data = load_input(INPUT_JSON)
    coords_map = _load_city_coords()
    cities = []
    for city_name in sorted(data["distance_matrix"].keys()):
        coords = coords_map.get(city_name, {"lat": 39.0, "lon": 35.0})
        cities.append({
            "id":          city_name,
            "ad":          city_name,
            "lat":         coords["lat"],
            "lon":         coords["lon"],
            "tm_var":      True,
            "tm_kapasite": 500000,
            "tir_yanasma": True,
        })
    return {"sehirler": cities}


@app.get("/api/vehicles")
def list_vehicles():
    """
    FIX #2 — Araç tipi bilgileri artık logiai_mvp_input.json'dan türetiliyor.
    JSON'da 'vehicle_types' anahtarı yoksa statik fallback kullanılır.
    """
    data = load_input(INPUT_JSON)
    if "vehicle_types" in data:
        return {"arac_tipleri": data["vehicle_types"]}

    # Fallback: cost_matrix'ten benzersiz araç tiplerini topla
    cost_matrix = data.get("cost_matrix", {})
    seen: set[str] = set()
    for origin_data in cost_matrix.values():
        for dest_data in origin_data.values():
            seen.update(dest_data.keys())

    # Statik kapasite tablosu (JSON'dan okunamazsa)
    _static_caps = {
        "Tır": {"id": "TIR", "ad": "Tır", "kapasite_desi": 22400, "sabit_maliyet": 7000.0, "km_basi_maliyet": 13.0, "tir_yanasma_gerekli": True},
        "Kamyon": {"id": "KAM", "ad": "Kamyon", "kapasite_desi": 12000, "sabit_maliyet": 5000.0, "km_basi_maliyet": 10.0, "tir_yanasma_gerekli": False},
        "Hafif Kamyon": {"id": "HAF", "ad": "Hafif Kamyon", "kapasite_desi": 7200, "sabit_maliyet": 5000.0, "km_basi_maliyet": 10.0, "tir_yanasma_gerekli": False},
        "Kamyonet": {"id": "KMT", "ad": "Kamyonet", "kapasite_desi": 5600, "sabit_maliyet": 3750.0, "km_basi_maliyet": 6.0, "tir_yanasma_gerekli": False},
    }
    arac_tipleri = [_static_caps[t] for t in seen if t in _static_caps]
    if not arac_tipleri:
        arac_tipleri = list(_static_caps.values())

    return {"arac_tipleri": arac_tipleri}


# FIX #33 — pagination eklendi
@app.get("/api/demand")
def get_demand(
    tarih: Optional[str] = Query(None),
    sehir: Optional[str] = Query(None),
    limit: int = Query(500, ge=1, le=5000, description="Sayfa başı kayıt"),
    offset: int = Query(0, ge=0, description="Başlangıç offset"),
):
    """Talep verisi sorgulama — sayfalı (limit/offset)"""
    # FIX #21 — index'li erişim
    if tarih:
        demand_data = list(_build_demand_index().get(tarih, []))
    else:
        demand_data = load_demand()

    if sehir:
        demand_data = [r for r in demand_data if r["gonderen_id"] == sehir or r["alan_id"] == sehir]

    total = len(demand_data)
    page = demand_data[offset: offset + limit]
    return {"toplam_kayit": total, "limit": limit, "offset": offset, "talepler": page}


# ──────────────────────────────────────────────
#  Async Optimizasyon — Redis Polling
# ──────────────────────────────────────────────

class AsyncJobResponse(BaseModel):
    model_config = ConfigDict(strict=False)

    job_id: str
    status: str


# FIX #44 — BackgroundTasks ile worker
# FIX #10 — Semaphore: maksimum eş zamanlı iş sınırı
@app.post("/api/optimize/async", response_model=AsyncJobResponse)
def optimize_async(req: OptimizeRequest, background_tasks: BackgroundTasks):
    """
    Optimizasyonu arka planda başlatır; arayüz kilitlenmez.

    Hemen job_id döner. İstemci /api/jobs/{job_id} adresini
    periyodik olarak sorgulayarak durumu takip eder.

    İş durumları: PENDING → RUNNING → COMPLETED | FAILED

    FIX #39 — NOT: uvicorn --workers > 1 ile çalıştırıldığında her worker
    kendi izole ThreadPool havuzuna sahiptir. Gerçek multi-worker ortamı için
    Celery veya ARQ kullanılması önerilir.
    """
    if not os.path.exists(INPUT_JSON):
        raise HTTPException(404, f"Girdi dosyası bulunamadı: {INPUT_JSON}")

    try:
        data = load_input(INPUT_JSON)
    except DataContractError as e:
        raise HTTPException(400, f"Veri sözleşmesi hatası: {e}")

    dates = available_dates(data)
    if req.tarih not in dates:
        raise HTTPException(
            404,
            f"{req.tarih} tarihi için talep verisi bulunamadı. "
            f"Mevcut tarihler: {dates}",
        )

    # FIX #10 — Semaphore kapıda — doluysa 429
    if not _semaphore.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail=f"Maksimum eş zamanlı iş sayısına ({_MAX_CONCURRENT_JOBS}) ulaşıldı. Lütfen bekleyin.",
        )

    job_id = create_job()

    # FIX #44 — BackgroundTasks (FastAPI native)
    def _worker():
        set_running(job_id)
        try:
            start = datetime.datetime.now(datetime.timezone.utc)  # FIX #31
            result = _run_pipeline(data, req.tarih, time_limit_sec=req.time_limit)
            elapsed = (datetime.datetime.now(datetime.timezone.utc) - start).total_seconds()
            result["calisma_suresi_sn"] = round(elapsed, 3)
            set_completed(job_id, result)
        except Exception as exc:
            logger.exception("Worker hatası job_id=%s", job_id)
            # FIX #6 — set_failed try/except ile guard
            try:
                set_failed(job_id, str(exc))
            except Exception as inner_exc:
                logger.error("set_failed başarısız job_id=%s: %s", job_id, inner_exc)
        finally:
            _semaphore.release()

    background_tasks.add_task(_worker)
    return {"job_id": job_id, "status": "PENDING"}


@app.get("/api/jobs/{job_id}")
def get_job_status(job_id: str):
    """
    İş durumunu döner.

    Dönen alan "status": PENDING | RUNNING | COMPLETED | FAILED
    COMPLETED ise "result" alanı OptimizeResponse şemasıyla aynıdır.
    FAILED ise "error" alanı hata mesajını içerir.
    """
    job = _get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job bulunamadı veya süresi doldu (TTL: 1 saat).")
    return job


if __name__ == "__main__":
    import uvicorn
    # FIX #39 uyarısı: --workers > 1 için Celery/ARQ kullanın
    uvicorn.run(app, host="0.0.0.0", port=8000, workers=1)
