"""
FastAPI Gateway — Lojistik Optimizasyon Sistemi
Person D: Sistem Mimarı ve Arayüz Geliştiricisi

Modüller:
- /api/optimize  → OR-Tools rota optimizasyonu
- /api/predict   → LSTM talep tahmini (placeholder)
- /api/fleet     → Filo atama durumu
- /api/tm-status → Transfer merkezi kapasite izleme
- /api/excel     → Excel çıktı üretimi
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional
import json, os, datetime, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Proje kök dizinini sys.path'e ekle (container içinde src/ doğrudan erişilebilir)
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from optimization.greedy import run_greedy_assignment
from optimization.vrp_solver import run_spot_vrp
from models.data_types import PipelineResult, RentalAssignment, SpotAssignment
from utils.data_loader import load_input, available_dates, DataContractError
from app.job_manager import create_job, set_running, set_completed, set_failed, get_job as _get_job, get_job_for_date as _get_job_for_date

_executor = ThreadPoolExecutor(max_workers=4)

app = FastAPI(
    title="Lojistik Optimizasyon API",
    description="Teknofest LogiAI — Karar Destek Sistemi Backend",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Proje kök dizini: src/app/main.py → ../../ (proje kökü)
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(_PROJECT_ROOT, "data", "raw"))
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", os.path.join(_PROJECT_ROOT, "data", "processed"))
INPUT_JSON = os.environ.get("INPUT_JSON", os.path.join(DATA_DIR, "logiai_mvp_input.json"))

# ──────────────────────────────────────────────
#  Modeller (Pydantic) — Yeni pipeline çıktı şemasına uyumlu
# ──────────────────────────────────────────────

class OptimizeRequest(BaseModel):
    tarih: str = Field(..., description="Planlama tarihi (YYYY-MM-DD)")
    time_limit: int = Field(540, ge=1, description="OR-Tools zaman sınırı (saniye)")

class RentalAssignmentResponse(BaseModel):
    vehicle_id: str
    origin: str
    destination: str
    assigned_desi: float
    capacity_desi: float
    utilisation: float
    cost: float
    cost_type: str

class SpotAssignmentResponse(BaseModel):
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
    tm_id: str
    tm_ad: str
    kapasite: int
    yuk: int
    asim: int
    asim_maliyet: float

class FleetVehicle(BaseModel):
    arac_id: str
    tip: str
    sabit_gunluk: float
    aktif: bool
    rota: Optional[str] = None
    doluluk_pct: float = 0.0

# ──────────────────────────────────────────────
#  Yardımcı Fonksiyonlar
# ──────────────────────────────────────────────

_COORDS_XLSX = os.path.join(DATA_DIR, "Koordinatlar v2.xlsx")
_CITY_COORDS_CACHE: dict[str, dict] | None = None


def _load_city_coords() -> dict[str, dict]:
    """Koordinatlar v2.xlsx'ten şehir koordinatlarını okur; sonucu önbelleğe alır."""
    global _CITY_COORDS_CACHE
    if _CITY_COORDS_CACHE is not None:
        return _CITY_COORDS_CACHE
    coords: dict[str, dict] = {}
    try:
        from openpyxl import load_workbook
        wb = load_workbook(_COORDS_XLSX)
        ws = wb.active
        for row in ws.iter_rows(min_row=2, values_only=True):
            city, lat, lon = row[0], row[1], row[2]
            if city and lat is not None and lon is not None:
                coords[str(city)] = {"lat": float(lat), "lon": float(lon)}
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Koordinat dosyası okunamadı: %s", e)
    _CITY_COORDS_CACHE = coords
    return coords


def load_demand():
    """daily_demand'ı düz liste formatına çevirir (günlük_talep.csv uyumu)."""
    data = load_input(INPUT_JSON)
    daily_demand = data.get("daily_demand", {})
    dates_sorted = sorted(daily_demand.keys())
    rows = []
    for day_idx, tarih in enumerate(dates_sorted, 1):
        for origin, dests in daily_demand[tarih].items():
            for dest, desi in dests.items():
                if float(desi) > 0:
                    rows.append({
                        "tarih": tarih,
                        "gonderen_id": origin,
                        "alan_id": dest,
                        "talep_desi": str(int(float(desi))),
                        "gun": str(day_idx),
                    })
    return rows

def load_distance_matrix():
    """logiai_mvp_input.json'dan mesafe matrisini döner."""
    data = load_input(INPUT_JSON)
    return data.get("distance_matrix", {})

def load_travel_time_matrix():
    """Seyahat süresi matrisini mesafeden türetir (ort. 80 km/saat)."""
    dist = load_distance_matrix()
    return {
        origin: {dest: round(km / 80.0, 2) for dest, km in dests.items()}
        for origin, dests in dist.items()
    }

def get_demand_for_date(demand_data, date_str):
    """Tarihe ait talepleri filtrele"""
    return [r for r in demand_data if r["tarih"] == date_str]

def _run_pipeline(data: dict, date: str, time_limit_sec: int = 540) -> dict:
    """
    İki aşamalı optimizasyon boru hattını çalıştırır ve JSON-uyumlu dict döner.

    Aşama 1 → Greedy kiralık atama   (optimization.greedy)
    Aşama 2 → OR-Tools Open VRP      (optimization.vrp_solver)
    """
    # Aşama 1: Greedy Kiralık Atama
    rental_assignments_list, spill_demand = run_greedy_assignment(data, date)
    total_rental_cost = sum(a.cost for a in rental_assignments_list)

    # Aşama 2: Spot VRP + Fallback
    spot_assignments_list = run_spot_vrp(data, spill_demand, time_limit_sec)
    total_spot_cost = sum(a.cost for a in spot_assignments_list)

    # Çözücü Durum Kodu
    fallback_count = sum(1 for a in spot_assignments_list if a.is_fallback)

    if not spill_demand:
        solver_status = "NO_DEMAND"
    elif fallback_count > 0:
        solver_status = "FALLBACK"
    elif spot_assignments_list:
        solver_status = "FEASIBLE"
    else:
        solver_status = "OPTIMAL"

    # Atanamayan Talep Kontrolü
    assigned_spill: dict[tuple[str, str], float] = {}
    for a in spot_assignments_list:
        key = (a.origin, a.destination)
        assigned_spill[key] = assigned_spill.get(key, 0.0) + a.assigned_desi

    unassigned: dict[str, float] = {}
    for (o, d), desi in spill_demand.items():
        leftover = desi - assigned_spill.get((o, d), 0.0)
        if leftover > 1.0:
            unassigned[f"{o}_{d}"] = leftover

    return {
        "date": date,
        "solver_status": solver_status,
        "total_rental_cost": total_rental_cost,
        "total_spot_cost": total_spot_cost,
        "total_cost": total_rental_cost + total_spot_cost,
        "fallback_count": fallback_count,
        "unassigned_demand": unassigned,
        "rental_assignments": [
            {
                "vehicle_id":    a.vehicle_id,
                "origin":        a.origin,
                "destination":   a.destination,
                "assigned_desi": a.assigned_desi,
                "capacity_desi": a.capacity_desi,
                "utilisation":   round(a.utilisation_rate, 4),
                "cost":          a.cost,
                "cost_type":     a.cost_type,
            }
            for a in rental_assignments_list
        ],
        "spot_assignments": [
            {
                "vehicle_type":  a.vehicle_type,
                "origin":        a.origin,
                "destination":   a.destination,
                "assigned_desi": a.assigned_desi,
                "capacity_desi": a.capacity_desi,
                "utilisation":   round(a.utilisation_rate, 4),
                "cost":          a.cost,
                "route_path":    list(a.route_path),
                "source":        a.source,
            }
            for a in spot_assignments_list
        ],
    }

# calc_spot_cost ve select_spot_vehicle artık kullanılmıyor;
# spot araç seçimi OR-Tools tarafından pipeline içinde yapılıyor.

# ──────────────────────────────────────────────
#  Endpoints
# ──────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "sistem": "Lojistik Optimizasyon API",
        "versiyon": "1.0.0",
        "durum": "aktif",
        "zaman": datetime.datetime.now().isoformat(),
    }

@app.get("/api/health")
def health():
    return {"status": "healthy", "timestamp": datetime.datetime.now().isoformat()}

@app.post("/api/optimize", response_model=OptimizeResponse)
def optimize(req: OptimizeRequest):
    """
    İki Aşamalı Optimizasyon Motoru (Ön Rapor Uyumlu)

    Aşama 1 — Greedy Kiralık Filo Atama:
      Sabit rotalı kiralık araçları büyükten küçüğe doldurup kalan spot talebi
      minimize eder. O(n log n) karmaşıklık, milisaniyeler içinde tamamlanır.

    Aşama 2 — OR-Tools Spot VRP:
      Kalan talepler; araç kapasitesi, tır yanaşma (α_i), TM elleçleme (δ_i)
      ve SLA gecikme (γ_ij) kısıtlarıyla OR-Tools'a verilir.
      Süre aşımında Fallback (B Planı) devreye girer.
    """
    start = datetime.datetime.now()

    # JSON girdi dosyasını yükle
    if not os.path.exists(INPUT_JSON):
        raise HTTPException(404, f"Girdi dosyası bulunamadı: {INPUT_JSON}")

    try:
        data = load_input(INPUT_JSON)
    except DataContractError as e:
        raise HTTPException(400, f"Veri sözleşmesi hatası: {e}")

    # Tarih kontrolü
    dates = available_dates(data)
    if req.tarih not in dates:
        raise HTTPException(
            404,
            f"{req.tarih} tarihi için talep verisi bulunamadı. "
            f"Mevcut tarihler: {dates}"
        )

    # İki Aşamalı Pipeline
    result = _run_pipeline(data, req.tarih, time_limit_sec=req.time_limit)

    elapsed = (datetime.datetime.now() - start).total_seconds()
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

    # Basit ortalama + mevsimsel etki
    from collections import defaultdict
    city_demand = defaultdict(list)
    for r in demand_data:
        key = r["gonderen_id"] if sehir is None or r["gonderen_id"] == sehir else None
        if key:
            city_demand[key].append(int(r["talep_desi"]))

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
    """Kiralık filo durum raporu. tarih verilirse Redis'ten gerçek doluluk oranı döner."""
    data = load_input(INPUT_JSON)

    # Tarih için tamamlanmış job varsa vehicle_id → utilisation map'i kur
    utilisation_map: dict[str, float] = {}
    if tarih:
        job = _get_job_for_date(tarih)
        if job and job.get("status") == "COMPLETED":
            for a in job["result"].get("rental_assignments", []):
                vid = a["vehicle_id"]
                # Aynı araç birden fazla atamada olabilir; toplamı kapat 1.0'da
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

    target_date = tarih or datetime.datetime.now().strftime("%Y-%m-%d")

    # Her şehir için tüm tarihler üzerinden maksimum akışı kapasite tahmini olarak kullan
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

    # Seçili tarihteki akış
    date_flow: dict[str, float] = {}
    for origin, dests in daily_demand.get(target_date, {}).items():
        for dest, desi in dests.items():
            date_flow[origin] = date_flow.get(origin, 0.0) + float(desi)
            date_flow[dest]   = date_flow.get(dest,   0.0) + float(desi)

    result = []
    for city in sorted(city_max_flow.keys()):
        kapasite = int(city_max_flow[city] * 1.5)  # %50 tampon
        yuk      = int(date_flow.get(city, 0.0))
        asim     = max(0, yuk - kapasite)
        result.append(TMDurum(
            tm_id=city[:4].upper().replace("İ", "I").replace("Ş", "S").replace("Ç", "C").replace("Ğ", "G").replace("Ü", "U").replace("Ö", "O"),
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

    demand_data = load_demand()
    date_demands = get_demand_for_date(demand_data, tarih)

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
            (int(r["talep_desi"]) for r in date_demands
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
        ws2.cell(row=i, column=4, value=int(r["talep_desi"]))

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

    return FileResponse(output_path, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename=f"rapor_{tarih}.xlsx")

@app.get("/api/cities")
def list_cities():
    """Tüm şehir ve TM bilgisi — koordinatlar Koordinatlar v2.xlsx'ten okunur."""
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
    """Araç tipi bilgileri"""
    # parameters.json'a olan bağımlılık kaldırıldı, yeni pipeline kapasiteleri statik olarak dönülüyor.
    return {
        "arac_tipleri": [
            {"id": "TIR", "ad": "Tır", "kapasite_desi": 22400, "sabit_maliyet": 7000.0, "km_basi_maliyet": 13.0, "tir_yanasma_gerekli": True},
            {"id": "KAM", "ad": "Kamyon", "kapasite_desi": 12000, "sabit_maliyet": 5000.0, "km_basi_maliyet": 10.0, "tir_yanasma_gerekli": False},
            {"id": "HAF", "ad": "Hafif Kamyon", "kapasite_desi": 7200, "sabit_maliyet": 5000.0, "km_basi_maliyet": 10.0, "tir_yanasma_gerekli": False},
            {"id": "KMT", "ad": "Kamyonet", "kapasite_desi": 5600, "sabit_maliyet": 3750.0, "km_basi_maliyet": 6.0, "tir_yanasma_gerekli": False}
        ]
    }

@app.get("/api/demand")
def get_demand(
    tarih: Optional[str] = Query(None),
    sehir: Optional[str] = Query(None),
):
    """Talep verisi sorgulama"""
    demand_data = load_demand()
    if tarih:
        demand_data = [r for r in demand_data if r["tarih"] == tarih]
    if sehir:
        demand_data = [r for r in demand_data if r["gonderen_id"] == sehir or r["alan_id"] == sehir]
    return {"toplam_kayit": len(demand_data), "talepler": demand_data}


# ──────────────────────────────────────────────
#  Async Optimizasyon — Redis Polling
# ──────────────────────────────────────────────

class AsyncJobResponse(BaseModel):
    job_id: str
    status: str


@app.post("/api/optimize/async", response_model=AsyncJobResponse)
def optimize_async(req: OptimizeRequest):
    """
    Optimizasyonu arka planda başlatır; arayüz kilitlenmez.

    Hemen job_id döner. İstemci /api/jobs/{job_id} adresini
    periyodik olarak sorgulayarak durumu takip eder.

    İş durumları: PENDING → RUNNING → COMPLETED | FAILED
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

    job_id = create_job()

    def _worker():
        set_running(job_id)
        try:
            start = datetime.datetime.now()
            result = _run_pipeline(data, req.tarih, time_limit_sec=req.time_limit)
            elapsed = (datetime.datetime.now() - start).total_seconds()
            result["calisma_suresi_sn"] = round(elapsed, 3)
            set_completed(job_id, result)
        except Exception as exc:
            set_failed(job_id, str(exc))

    _executor.submit(_worker)
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
    uvicorn.run(app, host="0.0.0.0", port=8000)
