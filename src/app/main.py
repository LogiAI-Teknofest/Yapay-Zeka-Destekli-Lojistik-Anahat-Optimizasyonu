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

from fastapi import FastAPI, HTTPException, UploadFile, File, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional
import json, os, datetime, sys
from pathlib import Path

# Proje kök dizinini sys.path'e ekle (container içinde src/ doğrudan erişilebilir)
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.optimization.pipeline import run_optimization_pipeline

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
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(_PROJECT_ROOT, "data", "raw"))
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", os.path.join(_PROJECT_ROOT, "data", "processed"))

# ──────────────────────────────────────────────
#  Modeller (Pydantic)
# ──────────────────────────────────────────────

class OptimizeRequest(BaseModel):
    tarih: str = Field(..., description="Planlama tarihi (YYYY-MM-DD)")
    hedef_filo_kullanim: float = Field(0.7, ge=0, le=1, description="Kiralık filo kullanım oranı")
    spot_limit: int = Field(20, ge=0, description="Maks spot araç sayısı")
    sla_katsayi: float = Field(15.0, ge=0, description="SLA gecikme ceza katsayısı")
    ellemcele_katsayi: float = Field(8.0, ge=0, description="TM elleçleme aşım ceza katsayısı")

class OptimizeResponse(BaseModel):
    status: str
    toplam_maliyet: float
    kirali_maliyet: float
    spot_maliyet: float
    ceza_maliyet: float
    rotalar: list
    tm_durum: list
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
    start       = datetime.datetime.now()
    params      = load_params()
    demand_data = load_demand()
    dist_matrix = load_distance_matrix()
    time_matrix = load_travel_time_matrix()

    date_demands = get_demand_for_date(demand_data, req.tarih)
    if not date_demands:
        raise HTTPException(404, f"{req.tarih} tarihi için talep verisi bulunamadı")

    # O-D sözlüğü oluştur
    od_demands: dict = {}
    for r in date_demands:
        key = f"{r['gonderen_id']}-{r['alan_id']}"
        od_demands[key] = od_demands.get(key, 0) + int(r["talep_desi"])

    # Request'ten gelen ceza katsayılarını params üzerine uygula
    params["sla_parametreleri"]["gecikme_ceza_katsayisi"]          = req.sla_katsayi
    params["tm_ellemcele_parametreleri"]["asim_ceza_katsayisi"]    = req.ellemcele_katsayi
    params["optimizasyon"]["sure_limit_dk"]                        = 10  # şartname: 10 dk

    # ── İki Aşamalı Pipeline ──────────────────────────────────────────────────
    result = run_optimization_pipeline(od_demands, dist_matrix, time_matrix, params)

    rental_plan = result["rental_fleet_plan"]
    spot_plan   = result["spot_fleet_plan"]

    # ── Rota listesini birleştir ──────────────────────────────────────────────
    rotalar = []

    # Kiralık araç rotaları
    for v in rental_plan:
        for atama in v.get("atanan_rotalar", []):
            src, dst = atama["rota"].split("-", 1)
            rotalar.append({
                "arac_id":   v["arac_id"],
                "tip":       "kirali",
                "arac_tipi": v["tip"],
                "rota":      atama["rota"],
                "kaynak":    src,
                "hedef":     dst,
                "yuk_desi":  atama["yuk_desi"],
                "mesafe_km": dist_matrix.get(src, {}).get(dst, 0),
                "sure_saat": time_matrix.get(src, {}).get(dst, 0),
                "maliyet":   v["sabit_gunluk"],
                "sla_deadline": params["sla_parametreleri"]["uzun_hat_deadline_saat"]
                                if dist_matrix.get(src, {}).get(dst, 0) > 500
                                else params["sla_parametreleri"]["hat_basi_deadline_saat"],
                "gecikme_saat": 0.0,
                "sla_ceza":     0.0,
            })

    # Spot araç rotaları
    for i, r in enumerate(spot_plan.get("rotalar", []), 1):
        rotalar.append({
            "arac_id":    f"S{i}",
            "tip":        "spot",
            "arac_tipi":  r.get("arac_tipi", "?"),
            "rota":       r.get("rota", ""),
            "kaynak":     r.get("kaynak", ""),
            "hedef":      r.get("hedef", ""),
            "yuk_desi":   r.get("yuk_desi", 0),
            "mesafe_km":  r.get("mesafe_km", 0),
            "sure_saat":  0.0,
            "maliyet":    r.get("maliyet", 0),
            "sla_deadline": 0.0,
            "gecikme_saat": 0.0,
            "sla_ceza":     r.get("sla_ceza", 0),
        })

    # ── TM elleçleme durumu ──────────────────────────────────────────────────
    tm_durum = []
    tm_cities = [c for c in params["sehirler"] if c.get("tm_var")]
    for tm in tm_cities:
        total_flow = sum(
            int(r["talep_desi"])
            for r in date_demands
            if r["gonderen_id"] == tm["id"] or r["alan_id"] == tm["id"]
        )
        kapasite  = tm["tm_kapasite"]
        asim      = max(0, total_flow - kapasite)
        asim_cost = asim * req.ellemcele_katsayi
        tm_durum.append({
            "tm_id":       tm["id"],
            "tm_ad":       tm["ad"],
            "kapasite":    kapasite,
            "yuk":         total_flow,
            "asim":        asim,
            "asim_maliyet": round(asim_cost, 2),
        })

    elapsed = (datetime.datetime.now() - start).total_seconds()

    return OptimizeResponse(
        status="completed" if "Başarılı" in spot_plan.get("status", "") else spot_plan.get("status", "completed"),
        toplam_maliyet=result["toplam_maliyet"],
        kirali_maliyet=result["kirali_maliyet"],
        spot_maliyet=result["spot_maliyet"],
        ceza_maliyet=result["ceza_maliyet"],
        rotalar=rotalar,
        tm_durum=tm_durum,
        calisma_suresi_sn=round(elapsed, 3),
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
    uvicorn.run(app, host="0.0.0.0", port=8000)
