import json
from pathlib import Path

HERE = Path(__file__).parent
out = json.loads((HERE / "../data/processed/optimization_result.json").read_text(encoding="utf-8"))
inp = json.loads((HERE / "../data/processed/logiai_mvp_input.json").read_text(encoding="utf-8"))

r = out["results"][0]

print("=== GENEL ===")
print(f"Tarih         : {r['date']}")
print(f"Solver status : {r['solver_status']}")
print(f"Calisma suresi: {r['calisma_suresi_sn']}s")
print(f"Toplam maliyet: {r['total_cost']:,.0f} TL")
print()

rent = r["rental_assignments"]
print("=== KIRALIK (Asama 1 - Greedy) ===")
print(f"Atama sayisi  : {len(rent)} arac")
print(f"Toplam desi   : {sum(a['assigned_desi'] for a in rent):,.0f} desi")
print(f"Maliyet       : {r['total_rental_cost']:,.0f} TL")
print()

spot = r["spot_assignments"]
vrp_a  = [a for a in spot if a["source"] == "vrp"]
fall_a = [a for a in spot if a["source"] == "fallback"]
print("=== SPOT (Asama 2 - VRP + Fallback) ===")
print(f"Toplam atama  : {len(spot)}  (vrp:{len(vrp_a)}, fallback:{len(fall_a)})")
print(f"Toplam desi   : {sum(a['assigned_desi'] for a in spot):,.0f} desi")
print(f"Maliyet       : {r['total_spot_cost']:,.0f} TL")
print()

unassigned = r["unassigned_demand"]
print("=== ATANAMAYAN TALEPLER ===")
if unassigned:
    total_u = sum(unassigned.values())
    print(f"Guzergah sayisi: {len(unassigned)}")
    print(f"Toplam desi    : {total_u:,.0f} desi")
    for k, v in list(unassigned.items())[:10]:
        print(f"  {k}: {v:,.1f} desi")
else:
    print("TUM TALEPLER ATANDI (unassigned_demand bos) [OK]")
print()

day = inp["daily_demand"].get("2026-05-11", {})
total_demand = sum(d for origins in day.values() for d in origins.values())
route_count  = sum(len(v) for v in day.values())
total_assigned = sum(a["assigned_desi"] for a in rent) + sum(a["assigned_desi"] for a in spot)

print("=== KAPSAMA ANALIZI ===")
print(f"Girdi O-D cifti  : {route_count}")
print(f"Toplam talep desi: {total_demand:,.0f}")
print(f"Toplam atanan    : {total_assigned:,.0f}")
kapsama = total_assigned / total_demand * 100 if total_demand > 0 else 0
print(f"Kapsama orani    : %{kapsama:.1f}")
print()
print(f"JSON satirlari   : 1663 (sadece 1 tarih icin - {r['date']})")
print(f"137 tum tarih olsaydi: ~{1663 * 137:,} satir beklenir")
