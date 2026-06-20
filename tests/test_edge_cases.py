"""
LogiAI — Veri Yükleme Dayanıklılık Testi (Kişi C)
data_loader'ı kasıtlı bozuk JSON ile test eder.

Graceful Degradation sözleşmesi (Kaptan Kuralı #2):
  * İSKELET bozuksa (dosya yok / geçersiz JSON / zorunlu anahtar eksik)
    -> DataContractError fırlatılır.
  * ALT DAL bozuksa (eşleşmeyen şehir, negatif değer, None fiyat, bozuk tarih)
    -> ÇÖKMEZ; bozuk kayıt atlanır, sağlam veri yüklenir.

Proje kök dizininden çalıştır: python tests/test_edge_cases.py
"""

import copy
import io
import json
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# Minimum geçerli sözleşme — her test kendi bozuk kopyasını üretir.
# (Sentetik birim-test fixture'ı; gerçek parametre kaynağı değildir.)
_BASE = {
    "vehicles_info":   {"Tır": {"name": "Tır", "capacity_desi": 22400}},
    "spot_capacities": {"Tır": 22400},
    "distance_matrix": {"A": {"B": 100.0}, "B": {"A": 100.0}},
    "cost_matrix": {"A": {"B": {"Tır": {"kiralik": 500.0, "spot": 600.0}}}},
    "rental_routes": {"A_B": [{"id": "KIR_TIR_01", "vehicle_type": "Tır", "capacity_desi": 22400}]},
    "daily_demand": {"2026-05-23": {"A": {"B": 500.0}}},
}


def _write_tmp(data: dict) -> str:
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return path


def _header(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print("=" * 60)


# ── Senaryo 1: İSKELET BOZUK → DataContractError (fail-fast) ──────────────────

def test_iskelet_bozuk():
    from src.utils.data_loader import load_input, DataContractError

    _header("SENARYO 1: İskelet Bozuk → DataContractError (fail-fast)")

    print("\n  [1a] Zorunlu üst anahtar eksik ('distance_matrix' yok)")
    data = {k: v for k, v in _BASE.items() if k != "distance_matrix"}
    path = _write_tmp(data)
    try:
        load_input(path)
        print("  [FAIL]  DataContractError bekleniyor ama firlatilmadi!")
        assert False
    except DataContractError as e:
        print(f"  [OK]  Iskelet eksigi yakalandi: {e}")
    finally:
        os.unlink(path)

    print("\n  [1b] Bozuk JSON sözdizimi")
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("{bozuk: json, verisi")
    try:
        load_input(path)
        print("  [FAIL]  DataContractError bekleniyor ama firlatilmadi!")
        assert False
    except DataContractError as e:
        print(f"  [OK]  Bozuk JSON yakalandi: {e}")
    finally:
        os.unlink(path)

    print("\n  [1c] Dosya yok")
    try:
        load_input("/tmp/var_olmayan_logiai_12345.json")
        print("  [FAIL]  DataContractError bekleniyor ama firlatilmadi!")
        assert False
    except DataContractError as e:
        print(f"  [OK]  Olmayan dosya yakalandi: {e}")

    print("\n  [1d] vehicles_info dict degil (iskelet)")
    data = copy.deepcopy(_BASE)
    data["vehicles_info"] = ["bozuk"]
    path = _write_tmp(data)
    try:
        load_input(path)
        print("  [FAIL]  DataContractError bekleniyor ama firlatilmadi!")
        assert False
    except DataContractError as e:
        print(f"  [OK]  Tip hatasi yakalandi: {e}")
    finally:
        os.unlink(path)


# ── Senaryo 2: BOZUK ALT-VERİ → Çökmeden Atla (Graceful) ─────────────────────

def test_graceful_skip():
    from src.utils.data_loader import load_input

    _header("SENARYO 2: Bozuk Alt-Veri → Çökmeden Atla (Graceful)")

    print("\n  [2a] Negatif mesafe → o mesafe atlanir, yukleme surer")
    data = copy.deepcopy(_BASE)
    data["distance_matrix"]["A"]["B"] = -50.0
    path = _write_tmp(data)
    try:
        d = load_input(path)
        assert "B" not in d["distance_matrix"].get("A", {}), "negatif mesafe atlanmaliydi"
        print("  [OK]  Negatif mesafe atlandi, yukleme surdu")
    finally:
        os.unlink(path)

    print("\n  [2b] rental_routes gecersiz anahtar ('_' yok) → rota atlanir")
    data = copy.deepcopy(_BASE)
    data["rental_routes"]["GECERSIZANAHTAR"] = [{"id": "X", "capacity_desi": 100}]
    path = _write_tmp(data)
    try:
        d = load_input(path)
        assert "GECERSIZANAHTAR" not in d["rental_routes"], "gecersiz rota atlanmaliydi"
        assert "A_B" in d["rental_routes"], "saglam rota korunmaliydi"
        print("  [OK]  Gecersiz rota atlandi, saglam rota korundu")
    finally:
        os.unlink(path)

    print("\n  [2c] daily_demand'da distance_matrix'te olmayan sehir → satir atlanir")
    data = copy.deepcopy(_BASE)
    data["daily_demand"]["2026-05-23"]["BILINMEYEN"] = {"B": 100.0}
    path = _write_tmp(data)
    try:
        d = load_input(path)
        assert "BILINMEYEN" not in d["daily_demand"]["2026-05-23"], "bilinmeyen sehir atlanmaliydi"
        assert "A" in d["daily_demand"]["2026-05-23"], "saglam talep korunmaliydi"
        print("  [OK]  Bilinmeyen sehir atlandi, saglam talep korundu")
    finally:
        os.unlink(path)

    print("\n  [2d] rental_routes'ta distance_matrix'te olmayan sehir → rota atlanir")
    data = copy.deepcopy(_BASE)
    data["rental_routes"]["HAYALET_B"] = [
        {"id": "KIR_TIR_99", "vehicle_type": "Tır", "capacity_desi": 22400}
    ]
    path = _write_tmp(data)
    try:
        d = load_input(path)
        assert "HAYALET_B" not in d["rental_routes"], "hayalet sehirli rota atlanmaliydi"
        print("  [OK]  Tutarsiz rota atlandi")
    finally:
        os.unlink(path)

    print("\n  [2e] spot_capacities eksik → vehicles_info'dan türetilir")
    data = copy.deepcopy(_BASE)
    del data["spot_capacities"]
    path = _write_tmp(data)
    try:
        d = load_input(path)
        assert d["spot_capacities"].get("Tır") == 22400, "spot_capacities türetilmeliydi"
        print(f"  [OK]  spot_capacities türetildi: {d['spot_capacities']}")
    finally:
        os.unlink(path)

    print("\n  [2f] None fiyat → o cost kaydi atlanir, yukleme surer")
    data = copy.deepcopy(_BASE)
    data["cost_matrix"]["A"]["B"]["Tır"]["spot"] = None
    path = _write_tmp(data)
    try:
        d = load_input(path)
        # Tır kaydi gecersiz spot yuzunden atlanmali; yukleme yine de basarili
        assert "Tır" not in d["cost_matrix"].get("A", {}).get("B", {}), "None fiyatli kayit atlanmaliydi"
        print("  [OK]  None fiyatli cost kaydi atlandi, yukleme surdu")
    finally:
        os.unlink(path)


# ── Senaryo 3: Kiralık Fiyat Anahtarı Varyantları ────────────────────────────

def test_kiralik_varyant():
    from src.utils.data_loader import load_input

    _header("SENARYO 3: Kiralık Fiyat Anahtarı Varyantları (kiralik / kiralık)")

    print("\n  [3a] 'kiralık' (Türkçe ı) varyanti kabul edilmeli")
    data = copy.deepcopy(_BASE)
    tir = data["cost_matrix"]["A"]["B"]["Tır"]
    tir["kiralık"] = tir.pop("kiralik")   # kiralik -> kiralık
    path = _write_tmp(data)
    try:
        d = load_input(path)
        assert "Tır" in d["cost_matrix"]["A"]["B"], "kiralık varyanti gecerli sayilmaliydi"
        print("  [OK]  'kiralık' varyanti gecerli sayildi, yuklendi")
    finally:
        os.unlink(path)

    print("\n  [3b] Hicbir kiralik anahtari yok → cost kaydi atlanir")
    data = copy.deepcopy(_BASE)
    del data["cost_matrix"]["A"]["B"]["Tır"]["kiralik"]
    path = _write_tmp(data)
    try:
        d = load_input(path)
        assert "Tır" not in d["cost_matrix"].get("A", {}).get("B", {}), "kiralik'siz kayit atlanmaliydi"
        print("  [OK]  Kiralik anahtari olmayan kayit atlandi")
    finally:
        os.unlink(path)


# ── Runner ────────────────────────────────────────────────────────────────────

def run_stress_test():
    print("\n" + "=" * 60)
    print("  LogiAI — Veri Yükleme Dayanıklılık Testi (Graceful)")
    print("  src.utils.data_loader :: load_input, DataContractError")
    print("=" * 60)

    senaryolar = [
        ("Iskelet Bozuk -> raise",          test_iskelet_bozuk),
        ("Bozuk Alt-Veri -> graceful skip", test_graceful_skip),
        ("Kiralik Fiyat Varyantlari",       test_kiralik_varyant),
    ]

    basarili, basarisiz = [], []
    for ad, fn in senaryolar:
        try:
            fn()
            basarili.append(ad)
        except AssertionError as e:
            basarisiz.append((ad, f"Assertion: {e}"))
            print(f"\n  [FAIL] {ad}: {e}")
        except Exception as e:
            basarisiz.append((ad, f"{type(e).__name__}: {e}"))
            print(f"\n  [ERROR] {ad}: {type(e).__name__}: {e}")

    print("\n" + "=" * 60)
    print("  SONUCLAR")
    print("=" * 60)
    for ad in basarili:
        print(f"  [PASS]  {ad}")
    for ad, neden in basarisiz:
        print(f"  [FAIL]  {ad} — {neden}")
    print(f"\n  {len(basarili)}/{len(senaryolar)} senaryo gecti")
    print("=" * 60)
    return len(basarisiz) == 0


if __name__ == "__main__":
    import logging
    # FIX (Kural 3): basicConfig yalnızca script doğrudan çalıştırıldığında.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s [%(name)s]: %(message)s")
    ok = run_stress_test()
    sys.exit(0 if ok else 1)
