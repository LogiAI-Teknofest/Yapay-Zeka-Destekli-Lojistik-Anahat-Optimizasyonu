from __future__ import annotations

import json
import math
import unicodedata
from pathlib import Path

import pandas as pd

try:
    from .data_preprocessing_analysis import (
        PROJECT_ROOT,
        excel_serial_to_datetime,
        normalize_city_name,
        parse_decimal_number,
        read_raw_file,
        title_city_name,
    )
except ImportError:
    from data_preprocessing_analysis import (
        PROJECT_ROOT,
        excel_serial_to_datetime,
        normalize_city_name,
        parse_decimal_number,
        read_raw_file,
        title_city_name,
    )


RAW_DIR = PROJECT_ROOT / "data" / "raw"
# Kaptan yapısı: üretilen JSON girdi sözleşmesi data/processed/ altına yazılır
# (data/raw yalnızca ham giriş Excel'lerini barındırır).
OUTPUT_PATH = PROJECT_ROOT / "data" / "processed" / "logiai_mvp_input.json"
DEFAULT_TRANSFER_CENTER_CAPACITY_DESI = 10_000_000
DEFAULT_TIR_ALLOWED = True

VEHICLE_TYPE_ALIASES = {
    "hafif kamyon": "HAF",
    "hafif_kamyon": "HAF",
    "hafifkamyon": "HAF",
    "tir": "TIR",
    "tır": "TIR",
    "kamyonet": "KMT",
    "kamyon": "KAM",
}

VEHICLE_TYPE_NAMES = {
    "HAF": "Hafif Kamyon",
    "TIR": "Tır",
    "KAM": "Kamyon",
    "KMT": "Kamyonet",
}
def generate_forecast_and_excel(demand_df: pd.DataFrame, project_root: Path) -> pd.DataFrame:
    """
    Geçmiş desi talep verilerinden hareketle 11-17 Mayıs haftası için tahmin üretir
    ve istenen 'Tahminlenen Talep' Excel çıktısını kaydeder.
    
    [OPTIMIZED VECTORS] Gruplanmış baseline verilerini hızlı erişim için çoklu indekse (MultiIndex)
    çevirerek iç içe döngü yavaşlamasını engeller.
    """
    # 1. Aşama: Geçmiş verinin haftanın gününü (Day of Week) buluyoruz
    demand_df['datetime'] = pd.to_datetime(demand_df['date'])
    demand_df['day_of_week'] = demand_df['datetime'].dt.dayofweek
    
    # Rota ve gün bazında ortalama talep hacmini (baseline) hesaplıyoruz
    baseline = demand_df.groupby(['origin', 'destination', 'day_of_week'])['desi'].mean().reset_index()
    
    # PERFORMANS FIX: Sürekli df filtrelemek yerine hızlı arama için (origin, destination, day_of_week) key'li bir dict yapıyoruz
    baseline_dict = baseline.set_index(['origin', 'destination', 'day_of_week'])['desi'].to_dict()
    
    # 2. Aşama: Hedef Tahmin Haftasını oluşturma (11 Mayıs - 17 Mayıs)
    target_dates = pd.date_range(start="2026-05-11", end="2026-05-17")
    forecast_records = []
    
    # Tüm aktif rotaları çekelim
    routes = demand_df[['origin', 'destination']].drop_duplicates().values  # Hız için numpy array'e çevirdik
    total_routes = len(routes)
    print(f"[BILGI] Toplam {total_routes} benzersiz rota için 7 günlük tahmin üretiliyor, lütfen bekleyin...")
    
    for target_date in target_dates:
        dow = target_date.dayofweek
        date_str = target_date.strftime('%Y-%m-%d')
        print(f"  -> {date_str} tarihi hesaplanıyor...") 
        
        for origin, destination in routes:
            # Hızlı sözlük araması (O(1) Karmaşıklığı)
            predicted_desi = baseline_dict.get((origin, destination, dow), 0.0)
            
            if predicted_desi > 0:
                forecast_records.append({
                    "Tarih": date_str,
                    "Çıkış TM": origin,
                    "Varış TM": destination,
                    "Tahmin Edilen Desi": round(float(predicted_desi), 2)
                })
                
    forecast_df = pd.DataFrame(forecast_records)
    
    # 3. Aşama: Şartnamede İstenen Excel Çıktısının Üretilmesi
    output_excel_path = project_root / "data" / "processed" / "Tahminlenen_Talep.xlsx"
    output_excel_path.parent.mkdir(parents=True, exist_ok=True)
    # Kaptan yapısı: 1. jüri teslimatı proje kökünde sabit adla durur.
    output_excel_path = project_root / "1_Tahmin_Talep_Ciktisi.xlsx"
    forecast_df.to_excel(output_excel_path, index=False)
    print(f"[BAŞARI] Şartname uyumlu 'Tahminlenen Talep' Excel'i üretildi: {output_excel_path}")
    
    return forecast_df

def normalize_key(value: object) -> str:
    text = "" if pd.isna(value) else str(value)
    text = text.strip().replace("ı", "i").replace("İ", "i")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(text.casefold().split())


def find_raw_excel(*keywords: str) -> Path:
    normalized_keywords = [normalize_key(keyword) for keyword in keywords]
    for path in RAW_DIR.glob("*.xlsx"):
        name = normalize_key(path.stem)
        if all(keyword in name for keyword in normalized_keywords):
            return path
    raise FileNotFoundError(f"Raw Excel dosyası bulunamadı: {keywords}")


def find_column(df: pd.DataFrame, *needles: str) -> object:
    normalized_needles = [normalize_key(needle) for needle in needles]
    for col in df.columns:
        label = normalize_key(col)
        if all(needle in label for needle in normalized_needles):
            return col
    raise KeyError(f"Kolon bulunamadı: {needles}")


def city_display(value: object) -> str:
    return title_city_name(normalize_city_name(value))


def normalize_vehicle_type(value: object) -> str | object:
    """
    Araç tipini standart kodlara (TIR, KAM, HAF, KMT) dönüştürür.
    Eğer bilinmeyen bir tip gelirse çökmez, pd.NA döner ve sistemin devam etmesini sağlar.
    """
    if pd.isna(value):
        return pd.NA
        
    key = normalize_key(value).replace(" ", "_")
    if key in VEHICLE_TYPE_ALIASES:
        return VEHICLE_TYPE_ALIASES[key]

    compact_key = key.replace("_", "")
    if compact_key in VEHICLE_TYPE_ALIASES:
        return VEHICLE_TYPE_ALIASES[compact_key]

    # #57 & #51 ÇÖZÜMÜ: Çökmüyoruz, ancak sessizce kaybolmaması için log basıyoruz.
    print(f"[UYARI / WARNING] Bilinmeyen veya bozuk araç tipi tespit edildi, satır atlanacak: '{value}'")
    return pd.NA

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_km = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return radius_km * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def load_coordinates() -> dict[str, dict[str, float]]:
    coordinates_path = RAW_DIR / "Koordinatlar v2.xlsx"
    if not coordinates_path.exists():
        coordinates_path = find_raw_excel("koordinat", "v2")
    df = read_raw_file(coordinates_path)
    city_col = find_column(df, "transfer")
    lat_col = find_column(df, "enlem")
    lon_col = find_column(df, "boylam")

    coords: dict[str, dict[str, float]] = {}
    for _, row in df.iterrows():
        city = city_display(row[city_col])
        lat = parse_decimal_number(pd.Series([row[lat_col]])).iloc[0]
        lon = parse_decimal_number(pd.Series([row[lon_col]])).iloc[0]
        if city and pd.notna(lat) and pd.notna(lon):
            coords[city] = {"lat": float(lat), "lon": float(lon)}
    return coords


def load_vehicle_costs() -> pd.DataFrame:
    df = read_raw_file(find_raw_excel("arac", "kapasite"))
    
    df_costs = pd.DataFrame(
        {
            "vehicle_type": df[find_column(df, "arac")].map(normalize_vehicle_type),
            "capacity_desi": parse_decimal_number(df[find_column(df, "kapasite")]),
            "rental_fixed": parse_decimal_number(df[find_column(df, "kiralik", "gunluk")]),
            "rental_km": parse_decimal_number(df[find_column(df, "kiralik", "kilometre")]),
            "spot_fixed": parse_decimal_number(df[find_column(df, "spot", "sabit")]),
            "spot_km": parse_decimal_number(df[find_column(df, "spot", "kilometre")]),
        }
    )
    
    # 1. Aşama: normalize_vehicle_type tarafından bilinmediği için NA işaretlenen satırları eliyoruz (#57 & #51)
    df_costs = df_costs.dropna(subset=["vehicle_type"])
    
    # 2. Aşama: Maliyet veya kapasite kolonlarında NaN (bozuk/boş veri) kontrolü (#Fail-Loud)
    maliyet_kolonlari = ["capacity_desi", "rental_fixed", "rental_km", "spot_fixed", "spot_km"]
    
    for col in maliyet_kolonlari:
        missing_mask = df_costs[col].isna()
        if missing_mask.any():
            corrupted_types = df_costs.loc[missing_mask, "vehicle_type"].tolist()
            # Sessizce yutmak yerine terminale hata/uyarı basıyoruz
            print(f"[HATA / ERROR] '{col}' kolonunda eksik/bozuk veri tespit edildi! "
                  f"Etkilenen araç tipleri: {corrupted_types}")
            
            # Kritik veri kaybını önlemek için varsayılan değer atayabilir veya hata fırlatabiliriz.
            # MVP güvencesi için 0 ile doldurup devam ediyoruz (veya raise ValueError yapabilirsin)
            df_costs[col] = df_costs[col].fillna(0.0)

    # 3. Aşama: Son Kontrol (Kritik 4 araç tipinin çıktıda var olduğunun doğrulanması)
    existing_types = set(df_costs["vehicle_type"].dropna().unique())
    required_types = {"TIR", "KAM", "HAF", "KMT"}
    missing_types = required_types - existing_types
    
    if missing_types:
        raise ValueError(f"KRİTİK HATA: Proje için zorunlu olan araç tipleri eksik: {missing_types}. "
                         f"Lütfen Araç_Kapasite_Maliyet.xlsx dosyasını kontrol edin!")

    return df_costs


# REVIZYON: coords haritası parametre olarak eklendi, böylece koordinatı olmayan şehir içeren satırlar elenecek.
def load_rental_vehicles(vehicle_costs: pd.DataFrame, coords: dict[str, dict[str, float]]) -> pd.DataFrame:
    df = read_raw_file(find_raw_excel("kiralik", "arac"))
    origin_col = find_column(df, "cikis")
    destination_col = find_column(df, "varis")
    count_col = find_column(df, "arac", "sayisi")
    type_col = find_column(df, "arac", "turu")

    rental = pd.DataFrame(
        {
            "origin": df[origin_col].map(city_display),
            "destination": df[destination_col].map(city_display),
            "vehicle_count": parse_decimal_number(df[count_col]).fillna(0).astype(int),
            "vehicle_type": df[type_col].map(normalize_vehicle_type),
        }
    )
    capacities = vehicle_costs.set_index("vehicle_type")["capacity_desi"].to_dict()
    rental["capacity_desi"] = rental["vehicle_type"].map(capacities)
    
    # Boş string temizliği
    rental[["origin", "destination"]] = rental[["origin", "destination"]].replace("", pd.NA)
    rental = rental.dropna(subset=["origin", "destination", "vehicle_type", "capacity_desi"])

    # BEKLENEN DURUM FİLTRESİ: Kalkış veya varış şehri koordinat tablosunda yoksa o satırı eliyoruz.
    valid_origin = rental["origin"].isin(coords.keys())
    valid_dest = rental["destination"].isin(coords.keys())
    rental = rental[valid_origin & valid_dest]

    return rental


# REVIZYON: coords haritası parametre olarak eklendi, böylece koordinatı olmayan şehir içeren satırlar elenecek.
def load_desi_demand(coords: dict[str, dict[str, float]]) -> pd.DataFrame:
    df = read_raw_file(find_raw_excel("desi", "talep"))
    origin_col = find_column(df, "cikis")
    destination_col = find_column(df, "varis")
    date_col = find_column(df, "tarih")
    desi_col = find_column(df, "desi")

    demand = pd.DataFrame(
        {
            "origin": df[origin_col].map(city_display),
            "destination": df[destination_col].map(city_display),
            "date": excel_serial_to_datetime(df[date_col]),
            "desi": parse_decimal_number(df[desi_col]),
        }
    )
    demand = demand.dropna(subset=["date", "desi"])
    demand = demand[demand["desi"] > 0]
    
    # Boş string temizliği
    demand[["origin", "destination"]] = demand[["origin", "destination"]].replace("", pd.NA)
    demand = demand.dropna(subset=["origin", "destination"])
    
    # BEKLENEN DURUM FİLTRESİ: Kalkış veya varış şehri koordinat tablosunda yoksa o satırı eliyoruz.
    valid_origin = demand["origin"].isin(coords.keys())
    valid_dest = demand["destination"].isin(coords.keys())
    demand = demand[valid_origin & valid_dest]

    demand["date"] = demand["date"].dt.date.astype(str)
    return demand


# REVIZYON: Bu fonksiyon artık sadece raporlama/loglama amaçlı eksik şehir tespiti yapacak.
def validate_coordinates(coords: dict[str, dict[str, float]], *tables: pd.DataFrame) -> list[str]:
    required = set()
    for table in tables:
        for col in ["origin", "destination"]:
            if col in table.columns:
                required.update(table[col].dropna().astype(str).tolist())

    return sorted(city for city in required if city not in coords)


def build_distance_matrix(coords: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    matrix: dict[str, dict[str, float]] = {}
    for origin, origin_coord in sorted(coords.items()):
        matrix[origin] = {}
        for destination, destination_coord in sorted(coords.items()):
            if origin == destination:
                continue
            distance = haversine_km(
                origin_coord["lat"],
                origin_coord["lon"],
                destination_coord["lat"],
                destination_coord["lon"],
            )
            matrix[origin][destination] = round(distance, 3)
    return matrix


def build_cost_matrix(
    distance_matrix: dict[str, dict[str, float]],
    vehicle_costs: pd.DataFrame,
) -> dict[str, dict[str, dict[str, dict[str, float]]]]:
    cost_matrix: dict[str, dict[str, dict[str, dict[str, float]]]] = {}
    for origin, destinations in distance_matrix.items():
        cost_matrix[origin] = {}
        for destination, distance in destinations.items():
            cost_matrix[origin][destination] = {}
            for _, vehicle in vehicle_costs.iterrows():
                vehicle_type = str(vehicle["vehicle_type"])
                rental_cost = float(vehicle["rental_fixed"]) + distance * float(vehicle["rental_km"])
                spot_cost = float(vehicle["spot_fixed"]) + distance * float(vehicle["spot_km"])
                cost_matrix[origin][destination][vehicle_type] = {
                    "kiralik": round(rental_cost, 2),
                    "spot": round(spot_cost, 2),
                }
    return cost_matrix


def build_rental_routes(rental: pd.DataFrame) -> dict[str, list[dict[str, object]]]:
    routes: dict[str, list[dict[str, object]]] = {}
    counters: dict[str, int] = {}
    for _, row in rental.iterrows():
        route_key = f"{row['origin']}_{row['destination']}"
        routes.setdefault(route_key, [])
        vehicle_type = str(row["vehicle_type"])
        for _idx in range(int(row["vehicle_count"])):
            counters[vehicle_type] = counters.get(vehicle_type, 0) + 1
            routes[route_key].append(
                {
                    "id": f"KIR_{vehicle_type}_{counters[vehicle_type]:02d}",
                    "vehicle_type": vehicle_type,
                    "capacity_desi": int(row["capacity_desi"]),
                }
            )
    return routes


def build_transfer_centers(coords: dict[str, dict[str, float]]) -> dict[str, dict[str, object]]:
    return {
        city: {
            "lat": coord["lat"],
            "lon": coord["lon"],
            "max_capacity_desi": DEFAULT_TRANSFER_CENTER_CAPACITY_DESI,
            "tir_allowed": DEFAULT_TIR_ALLOWED,
        }
        for city, coord in sorted(coords.items())
    }

def build_vehicle_fixed_costs(vehicle_costs: pd.DataFrame) -> dict[str, float]:
    """
    VRP Solver ve Fallback mekanizmalarının simetrik çalışabilmesi için
    araç kodlarına karşılık gelen gerçek spot günlük sabit maliyetlerini üretir.
    """
    return {
        str(row["vehicle_type"]): round(float(row["spot_fixed"]), 2)
        for _, row in vehicle_costs.iterrows()
    }

def build_vehicles_info(vehicle_costs: pd.DataFrame) -> dict[str, dict[str, object]]:
    return {
        str(row["vehicle_type"]): {
            "name": VEHICLE_TYPE_NAMES.get(str(row["vehicle_type"]), str(row["vehicle_type"])),
            "capacity_desi": int(row["capacity_desi"]),
            "rental_fixed_daily_cost": round(float(row["rental_fixed"]), 2),
            "rental_cost_per_km": round(float(row["rental_km"]), 2),
            "spot_fixed_daily_cost": round(float(row["spot_fixed"]), 2),
            "spot_cost_per_km": round(float(row["spot_km"]), 2),
        }
        for _, row in vehicle_costs.iterrows()
    }


def build_daily_demand(demand: pd.DataFrame) -> dict[str, dict[str, dict[str, float]]]:
    grouped = (
        demand.groupby(["date", "origin", "destination"], as_index=False)["desi"]
        .sum()
        .sort_values(["date", "origin", "destination"])
    )
    daily_demand: dict[str, dict[str, dict[str, float]]] = {}
    for _, row in grouped.iterrows():
        daily_demand.setdefault(row["date"], {}).setdefault(row["origin"], {})[row["destination"]] = round(
            float(row["desi"]),
            3,
        )
    return daily_demand

def build_tir_yanasma(coords: dict) -> dict[str, bool]:
    """
    Her Transfer Merkezi için TIR yanaşma izin durumunu üretir.

    VERİ-DRIVEN: yalnızca koordinat verisinde açıkça 'tir_allowed' alanı
    geçen şehirler o değeri alır; aksi halde TIR serbesttir (True).

    Hardcoded şehir kısıtı (eski 'restricted_cities') KALDIRILDI:
    jüri MVP'de "Transfer merkezi kısıtı yoktur" dedi ve Q&A (21 Haz)
    ekiplerin şartname/veri setinde yer almayan kısıt/varsayım
    tanımlamasını beklemiyor. Dataset B gerçek 'tir_allowed' alanı
    sağlarsa burası otomatik olarak onu kullanır.
    """
    return {
        city: (bool(info["tir_allowed"]) if "tir_allowed" in info else True)
        for city, info in coords.items()
    }

def build_logiai_mvp_contract() -> dict[str, object]:
    coords = load_coordinates()
    vehicle_costs = load_vehicle_costs()
    
    # Ham tabloları (filtresiz) geçici okuyoruz (Eksik şehir tespiti için)
    raw_df_rental = read_raw_file(find_raw_excel("kiralik", "arac"))
    raw_df_demand = read_raw_file(find_raw_excel("desi", "talep"))
    
    temp_rental = pd.DataFrame({
        "origin": raw_df_rental[find_column(raw_df_rental, "cikis")].map(city_display),
        "destination": raw_df_rental[find_column(raw_df_rental, "varis")].map(city_display)
    })
    temp_demand = pd.DataFrame({
        "origin": raw_df_demand[find_column(raw_df_demand, "cikis")].map(city_display),
        "destination": raw_df_demand[find_column(raw_df_demand, "varis")].map(city_display)
    })

    missing_coordinates = validate_coordinates(coords, temp_rental, temp_demand)

    if missing_coordinates:
        print(f"\n[UYARI / WARNING] Koordinat tablosunda bulunmayan şehirler tespit edildi: {', '.join(missing_coordinates)}")
        print("[BILGI / INFO] Bu şehirleri içeren hatalı satırlar veri setinden elendi. Sistem çalışmaya devam ediyor...\n")

    rental = load_rental_vehicles(vehicle_costs, coords)
    demand = load_desi_demand(coords)

    # =========================================================================
    # TAHMİN AKIŞI UYARLAMASI (#Tahminlenen_Talep Excel'ini basar ve contract'a bağlar)
    # =========================================================================
    forecast_df = generate_forecast_and_excel(demand, PROJECT_ROOT)
    forecast_mapped = forecast_df.rename(columns={
        "Tarih": "date",
        "Çıkış TM": "origin",
        "Varış TM": "destination",
        "Tahmin Edilen Desi": "desi"
    })
    combined_demand = pd.concat([demand, forecast_mapped], ignore_index=True)
    # =========================================================================

    distance_matrix = build_distance_matrix(coords)
    contract = {}
    
    # #30 HARİTA FIX: API katmanının (/api/cities) aradığı ve haritadaki kaymayı 
    # engelleyen coğrafi enlem/boylam kırılım sözlüğü oluşturuluyor.
    city_coords_dict = {
        city: {"lat": info["lat"], "lon": info["lon"]} 
        for city, info in coords.items()
    }
    
    contract.update({
        "city_coords": city_coords_dict,                                  # <-- #30 Fix
        "transfer_centers": build_transfer_centers(coords),
        "vehicles_info": build_vehicles_info(vehicle_costs),              # <-- #32 Fix (Gerçek Maliyetler)
        "vehicle_fixed_costs": build_vehicle_fixed_costs(vehicle_costs),  # <-- #31 Fix (VRP Simetrisi)
        "tir_yanasma": build_tir_yanasma(coords),
        "distance_matrix": distance_matrix,
        "cost_matrix": build_cost_matrix(distance_matrix, vehicle_costs),
        "rental_routes": build_rental_routes(rental),                     # <-- #29 Fix ("kiralik" ASCII eşleşmesi)
        "daily_demand": build_daily_demand(combined_demand),              # <-- Tahmin Entegre Veri Beslemesi
    })
    return contract


def main() -> None:
    print("\n>>> LogiAI Preprocessing Islemi Basladi <<<\n")
    try:
        contract = build_logiai_mvp_contract()
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")

        print(f"LogiAI MVP JSON kaydedildi: {OUTPUT_PATH}")
        print(f"TM sayısı: {len(contract['distance_matrix'])}")
        print(f"Tarih sayısı: {len(contract['daily_demand'])}")
        print(f"Kiralık rota sayısı: {len(contract['rental_routes'])}")
        
    except Exception as e:
        import traceback
        print("\n!!! KRİTİK HATA OLUŞTU VE İŞLEM YARIDA KESİLDİ !!!")
        print(f"Hata Mesajı: {e}")
        print("\nHata Detayı (Traceback):")
        traceback.print_exc()
        print("===============================================\n")

if __name__ == "__main__":
    main()