"""
LogiAI — Veri Yükleme Stres Testi (Kisi C)
data_loader modülünü kasitli bozuk JSON verisiyle test eder.
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

import logging

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s [%(name)s]: %(message)s",
)

# Minimum geçerli sözleşme — her test kendi bozuk kopyasını üretir
_BASE = {
    "vehicles_info":   {"Tır": {"name": "Tır", "capacity_desi": 22400}},
    "distance_matrix": {"A": {"B": 100.0}},
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


# ── Senaryo 1: Eksik / Bozuk Üst Düzey Alan ──────────────────────────────────

def test_eksik_alan():
    from src.utils.data_loader import load_input, DataContractError

    _header("SENARYO 1: Eksik / Bozuk Üst Düzey Alan")

    print("\n  [1a] Zorunlu alan eksik ('distance_matrix' yok)")
    data = {k: v for k, v in _BASE.items() if k != "distance_matrix"}
    path = _write_tmp(data)
    try:
        load_input(path)
        print("  [FAIL]  DataContractError bekleniyor ama firlatilmadi!")
        assert False
    except DataContractError as e:
        print(f"  [OK]  DataContractError dogru yakalandi: {e}")
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
        print(f"  [OK]  Bozuk JSON DataContractError dogru yakalandi: {e}")
    finally:
        os.unlink(path)

    print("\n  [1c] Dosya yok")
    try:
        load_input("/tmp/var_olmayan_logiai_12345.json")
        print("  [FAIL]  DataContractError bekleniyor ama firlatilmadi!")
        assert False
    except DataContractError as e:
        print(f"  [OK]  Olmayan dosya DataContractError dogru yakalandi: {e}")


# ── Senaryo 2: Bozuk İç Yapı ve Tutarsız Veri ────────────────────────────────

def test_bozuk_ic_yapi():
    from src.utils.data_loader import load_input, DataContractError

    _header("SENARYO 2: Bozuk İç Yapı ve Mantıksal Tutarsızlık")

    print("\n  [2a] distance_matrix'te negatif mesafe")
    data = copy.deepcopy(_BASE)
    data["distance_matrix"]["A"]["B"] = -50.0
    path = _write_tmp(data)
    try:
        load_input(path)
        print("  [FAIL]  DataContractError bekleniyor ama firlatilmadi!")
        assert False
    except DataContractError as e:
        print(f"  [OK]  Negatif mesafe yakalandi: {e}")
    finally:
        os.unlink(path)

    print("\n  [2b] rental_routes'ta gecersiz anahtar formati ('_' yok)")
    data2 = copy.deepcopy(_BASE)
    data2["rental_routes"]["GECERSIZANAHTAR"] = [{"id": "KIR_TIR_99", "capacity_desi": 100}]
    path2 = _write_tmp(data2)
    try:
        load_input(path2)
        print("  [FAIL]  DataContractError bekleniyor ama firlatilmadi!")
        assert False
    except DataContractError as e:
        print(f"  [OK]  Gecersiz rota anahtari yakalandi: {e}")
    finally:
        os.unlink(path2)

    print("\n  [2c] daily_demand'da distance_matrix'te olmayan sehir (coherence)")
    data3 = copy.deepcopy(_BASE)
    data3["daily_demand"]["2026-05-23"]["BILINMEYEN_SEHIR"] = {"B": 100.0}
    path3 = _write_tmp(data3)
    try:
        load_input(path3)
        print("  [FAIL]  DataContractError bekleniyor ama firlatilmadi!")
        assert False
    except DataContractError as e:
        print(f"  [OK]  Tutarsiz sehir yakalandi: {e}")
    finally:
        os.unlink(path3)


# ── Runner ────────────────────────────────────────────────────────────────────

def run_stress_test():
    print("\n" + "=" * 60)
    print("  LogiAI — Veri Yükleme Stres Testi")
    print("  src.utils.data_loader :: load_input, DataContractError")
    print("=" * 60)

    senaryolar = [
        ("Eksik / Bozuk Ust Duzey Alan",    test_eksik_alan),
        ("Bozuk Ic Yapi ve Tutarsizlik",     test_bozuk_ic_yapi),
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
    print("  STRES TESTI SONUCLARI")
    print("=" * 60)
    for ad in basarili:
        print(f"  [PASS]  {ad}")
    for ad, neden in basarisiz:
        print(f"  [FAIL]  {ad} — {neden}")
    print(f"\n  {len(basarili)}/{len(senaryolar)} senaryo gecti")
    print("=" * 60)
    return len(basarisiz) == 0


if __name__ == "__main__":
    ok = run_stress_test()
    sys.exit(0 if ok else 1)
