import sys
from pathlib import Path

# pipeline.py dosyasının bulunduğu klasörün bir üstündeki (yani src/) klasörü arama yollarına ekler
sys.path.append(str(Path(__file__).resolve().parent.parent))

# Artık import işlemlerini sanki 'src' klasörünün içindeymişsin gibi doğrudan yapabilirsin:
from optimization.greedy_fleet import assign_rentals_greedy
from optimization.vrp_solver import solve_spot_vrp
import json

def run_optimization_pipeline(all_demands, rental_capacities):
    """
    ANA BORU HATTI: 
    Sırasıyla önce bedava kiralık araçları doldurur, 
    ardından arta kalan yükleri akıllı spot araç rotalayıcıya paslar.
    """
    print("\n[LogiAI] 1. Aşama: Kiralık Araç Atamaları Yapılıyor (Greedy)...")
    rental_plan, remaining_spot_demands = assign_rentals_greedy(all_demands, rental_capacities)
    
    print("[LogiAI] 2. Aşama: Spot Araç Optimizasyonu Başlıyor (OR-Tools)...")
    spot_plan = solve_spot_vrp(remaining_spot_demands)
    
    # İki aşamadan gelen sonuçları tek bir çatı altında birleştiriyoruz
    final_output = {
        "rental_fleet_plan": rental_plan,
        "spot_fleet_plan": spot_plan
    }
    return final_output

# ---- TEST ETME ALANI ----
# Bu dosya doğrudan çalıştırıldığında sistemimizi simüle eder.
if __name__ == '__main__':
    # 0. indeks Merkez Depo (Talep 0). Diğerleri transfer merkezlerinin talepleri.
    # Unutma: vrp_solver.py içinde 1 ve 3 numaralı düğümlere Tır giremez demiştik.
    # Ayrıca kiralık araçlar 1, 2, 3 ve 5'in yükünü bitireceği için spot araç sadece 4'e gidecek.
    test_demands = [0, 45, 120, 30, 200, 50] 
    test_rentals = [150, 100] # Elimizde iki kiralık araç var (biri 150, biri 100 kapasiteli)
    
    print("=== LogiAI Optimizasyon Sistemi Başlatılıyor ===")
    
    # Sistemi çalıştır
    nihai_plan = run_optimization_pipeline(test_demands, test_rentals)
    
    print("\n=== SİSTEMDEN DÖNEN NİHAİ PLAN ===")
    print(json.dumps(nihai_plan, indent=2, ensure_ascii=False))