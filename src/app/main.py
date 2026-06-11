"""
FastAPI Gateway — Lojistik Optimizasyon Sistemi
Person D: Sistem Mimarı ve Arayüz Geliştiricisi

Modüller:
- /api/optimize        → OR-Tools rota optimizasyonu (senkron)
- /api/optimize/start  → Asenkron job başlatıcı (pending/running/done/error)
- /api/optimize/status → Job durum sorgulama
- /api/optimize/async  → Dashboard uyumlu async başlatıcı (PENDING/COMPLETED/FAILED)
- /api/jobs/{job_id}   → Dashboard uyumlu job sorgulama (TTL: 1 saat)
- /api/optimize/stream → SSE gerçek zamanlı akış
- /api/predict         → LSTM talep tahmini (placeholder)
- /api/fleet           → Filo atama durumu
- /api/tm-status       → Transfer merkezi kapasite izleme
- /api/excel           → Excel çıktı üretimi
"""

 
from fastapi import FastAPI, HTTPException, UploadFile, File, Query
from fastapi.responses import StreamingResponse
 
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, Literal
import json, os, datetime, sys, uuid, threading, time
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
#  Async Job Store — Timeout-safe optimizasyon
#  Kişi D: Uzun süren OR-Tools çözümlerini arka planda çalıştırır
# ──────────────────────────────────────────────

_job_store: dict[str, dict] = {}
_job_lock = threading.Lock()

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

def load_params():
    with open(os.path.join(DATA_DIR, "parameters.json"), "r") as f:
        return json.load(f)

def load_demand():
    import csv
    rows = []
    path = os.path.join(DATA_DIR, "gunluk_talep.csv")
    with open(path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows

def load_distance_matrix():
    import csv
    path = os.path.join(DATA_DIR, "mesafe_matrisi.csv")
    with open(path, "r") as f:
        reader = csv.reader(f)
        header = next(reader)
        matrix = {}
        for row in reader:
            city = row[0]
            matrix[city] = {}
            for i, val in enumerate(row[1:], 1):
                matrix[city][header[i]] = float(val)
    return matrix

def load_travel_time_matrix():
    import csv
    path = os.path.join(DATA_DIR, "seyahat_suresi_saat.csv")
    with open(path, "r") as f:
        reader = csv.reader(f)
        header = next(reader)
        matrix = {}
        for row in reader:
            city = row[0]
            matrix[city] = {}
            for i, val in enumerate(row[1:], 1):
                matrix[city][header[i]] = float(val)
    return matrix

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

# ──────────────────────────────────────────────
#  Async Optimize Endpoints (Kişi D — Timeout-Safe)
#  Dashboard bu endpoint'leri kullanarak OR-Tools'un
#  9 dk'ya kadar süren çözümlerini timeout olmadan alır.
# ──────────────────────────────────────────────

def _job_worker(job_id: str, data: dict, date: str, time_limit: int):
    """Arka plan thread'inde pipeline çalıştırır."""
    try:
        with _job_lock:
            _job_store[job_id]["status"] = "running"
            _job_store[job_id]["started_at"] = datetime.datetime.now().isoformat()
        
        result = _run_pipeline(data, date, time_limit_sec=time_limit)
        
        with _job_lock:
            _job_store[job_id]["status"] = "done"
            _job_store[job_id]["result"] = result
            _job_store[job_id]["finished_at"] = datetime.datetime.now().isoformat()
    except Exception as e:
        with _job_lock:
            _job_store[job_id]["status"] = "error"
            _job_store[job_id]["error"] = str(e)
            _job_store[job_id]["finished_at"] = datetime.datetime.now().isoformat()


@app.post("/api/optimize/start")
def optimize_start(req: OptimizeRequest):
    """
    Kişi D — Asenkron Optimizasyon Başlatıcı

    Pipeline'ı arka plan thread'inde çalıştırır ve hemen bir job_id döner.
    Dashboard bu job_id ile /api/optimize/status/{job_id} üzerinden sonucu sorgular.
    OR-Tools 9 dk sürse bile HTTP bağlantısı kopmaz.
    """
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

    # Job oluştur ve arka planda başlat
    job_id = str(uuid.uuid4())
    with _job_lock:
        _job_store[job_id] = {
            "status": "pending",
            "tarih": req.tarih,
            "time_limit": req.time_limit,
            "created_at": datetime.datetime.now().isoformat(),
            "result": None,
            "error": None,
        }

    thread = threading.Thread(
        target=_job_worker,
        args=(job_id, data, req.tarih, req.time_limit),
        daemon=True,
    )
    thread.start()

    return {"job_id": job_id, "status": "pending"}


@app.get("/api/optimize/status/{job_id}")
def optimize_status(job_id: str):
    """
    Kişi D — Job Durum Sorgulama

    Dönen status değerleri:
    - pending  : İş henüz başlamadı
    - running  : OR-Tools çalışıyor
    - done     : Sonuç hazır (result alanında)
    - error    : Hata oluştu (error alanında)
    """
    with _job_lock:
        job = _job_store.get(job_id)

    if not job:
        raise HTTPException(404, f"Job bulunamadı: {job_id}")

    response = {
        "job_id": job_id,
        "status": job["status"],
        "tarih": job.get("tarih"),
        "created_at": job.get("created_at"),
    }

    if job["status"] == "done":
        response["result"] = job["result"]
        response["finished_at"] = job.get("finished_at")
    elif job["status"] == "error":
        response["error"] = job["error"]
        response["finished_at"] = job.get("finished_at")

    return response


# ──────────────────────────────────────────────
#  Async Endpoints — Dashboard Uyumluluk Katmanı
#  POST /api/optimize/async  →  job_id döner (PENDING/RUNNING/COMPLETED/FAILED)
#  GET  /api/jobs/{job_id}   →  job durumu + sonuç
#  Bu endpoint'ler _job_store'u paylaşır; SSE endpoint'inden bağımsız tüketilebilir.
# ──────────────────────────────────────────────

class AsyncJobResponse(BaseModel):
    job_id: str
    status: str


@app.post("/api/optimize/async", response_model=AsyncJobResponse)
def optimize_async(req: OptimizeRequest):
    """
    Dashboard Uyumlu Asenkron Optimizasyon Başlatıcı

    /api/optimize/start ile aynı mantığı kullanır ancak status değerleri
    dashboard'un beklediği büyük harf formatındadır:
    PENDING → RUNNING → COMPLETED | FAILED
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
            f"Mevcut tarihler: {dates}"
        )

    job_id = str(uuid.uuid4())
    with _job_lock:
        _job_store[job_id] = {
            "status": "PENDING",
            "tarih": req.tarih,
            "time_limit": req.time_limit,
            "created_at": datetime.datetime.now().isoformat(),
            "started_at": None,
            "result": None,
            "error": None,
        }

    def _async_worker(jid: str, d: dict, date: str, tl: int):
        with _job_lock:
            _job_store[jid]["status"] = "RUNNING"
            _job_store[jid]["started_at"] = datetime.datetime.now().isoformat()
        try:
            res = _run_pipeline(d, date, time_limit_sec=tl)
            with _job_lock:
                _job_store[jid]["status"] = "COMPLETED"
                _job_store[jid]["result"] = res
                _job_store[jid]["finished_at"] = datetime.datetime.now().isoformat()
        except Exception as exc:
            with _job_lock:
                _job_store[jid]["status"] = "FAILED"
                _job_store[jid]["error"] = str(exc)
                _job_store[jid]["finished_at"] = datetime.datetime.now().isoformat()

    threading.Thread(
        target=_async_worker,
        args=(job_id, data, req.tarih, req.time_limit),
        daemon=True,
    ).start()

    return {"job_id": job_id, "status": "PENDING"}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    """
    Dashboard Uyumlu Job Durum Sorgulama

    Dönen status değerleri: PENDING | RUNNING | COMPLETED | FAILED
    COMPLETED durumunda 'result' alanında optimizasyon sonucu bulunur.
    Job 1 saatten eski ise 404 döner (TTL: 1 saat).
    """
    with _job_lock:
        job = _job_store.get(job_id)

    if not job:
        raise HTTPException(404, "Job bulunamadı veya süresi doldu (TTL: 1 saat).")

    # TTL kontrolü — 1 saatten eski job'ları kaldır
    created = datetime.datetime.fromisoformat(job["created_at"])
    if (datetime.datetime.now() - created).total_seconds() > 3600:
        with _job_lock:
            _job_store.pop(job_id, None)
        raise HTTPException(404, "Job süresi doldu (TTL: 1 saat).")

    response: dict = {
        "job_id": job_id,
        "status": job["status"],
        "tarih": job.get("tarih"),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
    }
    if job["status"] == "COMPLETED":
        response["result"] = job["result"]
        response["finished_at"] = job.get("finished_at")
    elif job["status"] == "FAILED":
        response["error"] = job.get("error")
        response["finished_at"] = job.get("finished_at")

    return response


@app.post("/api/optimize/stream")
def optimize_stream(req: OptimizeRequest):
    """SSE endpoint — sonuç hazır olana kadar event akışı gönderir."""

    # 1) Girdi doğrulama (mevcut koddaki gibi)
    data = load_input(INPUT_JSON)
    dates = available_dates(data)
    if req.tarih not in dates:
        raise HTTPException(404, "Tarih bulunamadı")

    # 2) Event generator — pipeline'ı thread'de çalıştırır
    def event_generator():
        result_holder = {}
        error_holder = {}
        start_time = time.time()

        # Pipeline'ı arka plan thread'inde başlat
        def worker():
            try:
                result_holder["data"] = _run_pipeline(
                    data, req.tarih, req.time_limit
                )
            except Exception as e:
                error_holder["msg"] = str(e)

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        # Thread çalışırken her 5 sn'de heartbeat gönder
        while t.is_alive():
            elapsed = round(time.time() - start_time, 1)
            event = {"status": "running", "elapsed": elapsed}
            yield f"data: {json.dumps(event)}\n\n"
            time.sleep(5)  # 5 sn bekle

        # Thread bitti — sonucu veya hatayı gönder
        elapsed = round(time.time() - start_time, 1)
        if error_holder:
            event = {"status": "error", "error": error_holder["msg"], "elapsed": elapsed}
        else:
            event = {"status": "done", "result": result_holder["data"], "elapsed": elapsed}

        yield f"data: {json.dumps(event)}\n\n"

    # 3) StreamingResponse ile SSE döndür
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # nginx proxy varsa buffering kapatır
        },
    )


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
    params = load_params()

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
def fleet_status():
    """Kiralık filo durum raporu"""
    params = load_params()
    result = []
    for v in params["kirali_filo"]:
        result.append(FleetVehicle(
            arac_id=v["arac_id"],
            tip=v["tip"],
            sabit_gunluk=v["sabit_gunluk"],
            aktif=True,
            rota=", ".join(v["rotalar"]),
        ))
    return result

@app.get("/api/tm-status", response_model=list[TMDurum])
def tm_status(tarih: Optional[str] = Query(None)):
    """Transfer merkezi kapasite izleme"""
    params = load_params()
    demand_data = load_demand()
    
    target_date = tarih or datetime.datetime.now().strftime("%Y-%m-%d")
    date_demands = get_demand_for_date(demand_data, target_date)

    result = []
    for c in params["sehirler"]:
        if not c.get("tm_var"):
            continue
        total_flow = sum(
            int(r["talep_desi"])
            for r in date_demands
            if r["gonderen_id"] == c["id"] or r["alan_id"] == c["id"]
        )
        kapasite = c["tm_kapasite"]
        asim = max(0, total_flow - kapasite)
        result.append(TMDurum(
            tm_id=c["id"],
            tm_ad=c["ad"],
            kapasite=kapasite,
            yuk=total_flow,
            asim=asim,
            asim_maliyet=round(asim * 8.0, 2),
        ))
    return result

@app.post("/api/excel")
def generate_excel(tarih: str = Query(...)):
    """Optimizasyon sonuçlarını Excel olarak oluştur"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    params = load_params()
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

    row_idx = 2
    for v in params["kirali_filo"]:
        for rota in v["rotalar"]:
            src, dst = rota.split("-")
            tip_info = next(t for t in params["arac_tipleri"] if t["id"] == v["tip"])
            demand_val = next((int(r["talep_desi"]) for r in date_demands
                              if r["gonderen_id"] == src and r["alan_id"] == dst), 0)
            if demand_val == 0:
                continue
            dist_matrix = load_distance_matrix()
            time_matrix = load_travel_time_matrix()
            ws1.cell(row=row_idx, column=1, value=v["arac_id"])
            ws1.cell(row=row_idx, column=2, value="Kiralık " + v["tip"])
            ws1.cell(row=row_idx, column=3, value=rota)
            ws1.cell(row=row_idx, column=4, value=src)
            ws1.cell(row=row_idx, column=5, value=dst)
            ws1.cell(row=row_idx, column=6, value=min(demand_val, tip_info["kapasite_desi"]))
            ws1.cell(row=row_idx, column=7, value=dist_matrix.get(src, {}).get(dst, 0))
            ws1.cell(row=row_idx, column=8, value=time_matrix.get(src, {}).get(dst, 0))
            ws1.cell(row=row_idx, column=9, value=v["sabit_gunluk"])
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

    total_fixed = sum(v["sabit_gunluk"] for v in params["kirali_filo"])
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

    return {"status": "created", "dosya": output_path, "tarih": tarih}

@app.get("/api/cities")
def list_cities():
    """Tüm şehir ve TM bilgisi"""
    params = load_params()
    return {"sehirler": params["sehirler"]}

@app.get("/api/vehicles")
def list_vehicles():
    """Araç tipi bilgileri"""
    params = load_params()
    return {"arac_tipleri": params["arac_tipleri"]}

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

if __name__ == "__main__":
     
    import uvicorn
    uvicorn.run(app, host="[IP_ADDRESS]", port=8000)