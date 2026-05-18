"""
LogiAI — Veri Validasyon Stres Testi (Kisi C)
data_validation modülünü kasitli bozuk verilerle test eder.
Proje kök dizininden çalıştır: python tests/test_edge_cases.py
"""

import io
import os
import sys

# Proje kök dizinini sys.path'e ekle
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import logging

import pandas as pd

from src.preprocessing.data_validation import (
    validate_transfer_centers,
    validate_vehicles,
    validate_packages,
    MAX_SINGLE_DESI,
)

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s [%(name)s]: %(message)s",
)


def _header(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print("=" * 60)


def _print_errors(errors):
    if not errors:
        print("  [Hata yok]")
        return
    for err in errors:
        print(f"  [HATA]  {err}")


def _result_line(label: str, gecerli: int, hata: int):
    status = "OK" if gecerli >= 0 else "??"
    print(f"\n  [{status}]  Gecerli: {gecerli}  |  Yakalanan hata: {hata}")
    print(f"        -> {label}")


# ── Senaryo 1: Bozuk TM Verisi ────────────────────────────────────────────────

def test_bozuk_tm_verisi():
    _header("SENARYO 1: Bozuk TM (Transfer Merkezi) Verisi")

    print("\n  [1a] Format hatasi + negatif kapasite + duplikat")
    df = pd.DataFrame([
        {"TM_ID": "34_01",      "Capacity": 5000},   # GECERLI
        {"TM_ID": "INVALID_ID", "Capacity": 3000},   # Hata: XX_XX formati degil
        {"TM_ID": "06_01",      "Capacity": -500},   # Hata: negatif kapasite
        {"TM_ID": "07_01",      "Capacity": 0},      # Hata: sifir kapasite
        {"TM_ID": "34_01",      "Capacity": 4000},   # Hata: duplikat TM_ID
    ])
    clean, errors = validate_transfer_centers(df)
    _print_errors(errors)
    _result_line("Sadece '34_01' (5000) gecmeli", len(clean), len(errors))
    assert len(clean)  == 1, f"1 gecerli TM bekleniyor, gelen={len(clean)}"
    assert len(errors) == 4, f"4 hata bekleniyor, gelen={len(errors)}"

    print("\n  [1b] Eksik zorunlu sutun ('Capacity' yok)")
    df_eksik = pd.DataFrame([{"TM_ID": "34_01"}, {"TM_ID": "07_01"}])
    try:
        validate_transfer_centers(df_eksik)
        print("  [FAIL]  ValueError bekleniyor ama firlatilmadi!")
    except ValueError as e:
        print(f"  [OK]  ValueError dogru yakalandi: {e}")

    print("\n  [1c] Tamamen bos DataFrame")
    df_bos = pd.DataFrame(columns=["TM_ID", "Capacity"])
    clean_bos, errors_bos = validate_transfer_centers(df_bos)
    assert len(clean_bos) == 0 and len(errors_bos) == 0
    print(f"  [OK]  Bos DataFrame hata firlatmadi: {len(clean_bos)} gecerli, {len(errors_bos)} hata")

    print("\n  [1d] NaN kapasite")
    df_nan = pd.DataFrame([
        {"TM_ID": "35_01", "Capacity": 3000},
        {"TM_ID": "07_01", "Capacity": None},
    ])
    clean_nan, errors_nan = validate_transfer_centers(df_nan)
    _print_errors(errors_nan)
    _result_line("Sadece '35_01' gecmeli", len(clean_nan), len(errors_nan))
    assert len(clean_nan)  == 1
    assert len(errors_nan) == 1


# ── Senaryo 2: Bozuk Arac Verisi ─────────────────────────────────────────────

def test_bozuk_arac_verisi():
    _header("SENARYO 2: Bozuk Arac Verisi")

    print("\n  [2a] Gecersiz tip + negatif kapasite + duplikat")
    df = pd.DataFrame([
        {"Vehicle_ID": "V001", "Type": "Tir",          "Capacity": 3000},  # GECERLI
        {"Vehicle_ID": "V002", "Type": "UcakTipi",     "Capacity": 1500},  # Hata: gecersiz tip
        {"Vehicle_ID": "V003", "Type": "Kamyon",       "Capacity": -200},  # Hata: negatif
        {"Vehicle_ID": "V004", "Type": "Kamyonet",     "Capacity": 0},     # Hata: sifir
        {"Vehicle_ID": "V001", "Type": "Hafif Kamyon", "Capacity": 800},   # Hata: duplikat
    ])
    clean, errors = validate_vehicles(df)
    _print_errors(errors)
    _result_line("Sadece V001 (Tir) gecmeli", len(clean), len(errors))
    assert len(clean)  == 1, f"1 gecerli arac bekleniyor, gelen={len(clean)}"
    assert len(errors) == 4, f"4 hata bekleniyor, gelen={len(errors)}"

    print("\n  [2b] Eksik zorunlu sutun ('Type' yok)")
    df_eksik = pd.DataFrame([{"Vehicle_ID": "V001", "Capacity": 3000}])
    try:
        validate_vehicles(df_eksik)
        print("  [FAIL]  KeyError bekleniyor ama firlatilmadi!")
    except (KeyError, Exception) as e:
        print(f"  [OK]  Hata dogru yakalandi: {type(e).__name__}: {e}")


# ── Senaryo 3: Bozuk Paket Verisi ─────────────────────────────────────────────

def test_bozuk_paket_verisi():
    _header("SENARYO 3: Bozuk Paket Verisi")

    print("\n  [3a] Tum bozuk desi turleri")
    packages = [
        {"pkg_id": "PKG_001", "tm_id": "34_01", "desi": 50},      # GECERLI
        {"pkg_id": "PKG_002", "tm_id": "34_01", "desi": "Yuz"},   # Hata: string desi
        {"pkg_id": "PKG_003", "tm_id": "34_01", "desi": 0},        # Hata: sifir desi
        {"pkg_id": "PKG_004", "tm_id": "34_01", "desi": -10},      # Hata: negatif desi
        {"pkg_id": "PKG_005", "tm_id": "34_01", "desi": 999999},   # Hata: astronomik
        {"pkg_id": "PKG_006", "tm_id": "34_01", "desi": 120},      # GECERLI
    ]
    clean, errors = validate_packages(packages)
    _print_errors(errors)
    _result_line("PKG_001 ve PKG_006 gecmeli", len(clean), len(errors))
    assert len(clean)  == 2, f"2 gecerli paket bekleniyor, gelen={len(clean)}"
    assert len(errors) == 4, f"4 hata bekleniyor, gelen={len(errors)}"

    print("\n  [3b] Tanimsiz TM_ID")
    pkgs_bad_tm = [
        {"pkg_id": "P001", "tm_id": "34_01", "desi": 80},
        {"pkg_id": "P002", "tm_id": "99_99", "desi": 100},
        {"pkg_id": "P003", "tm_id": None,    "desi": 50},
    ]
    clean2, errors2 = validate_packages(pkgs_bad_tm)
    _print_errors(errors2)
    _result_line("Sadece P001 gecmeli", len(clean2), len(errors2))
    assert len(clean2)  == 1
    assert len(errors2) >= 2

    print("\n  [3c] Duplikat pkg_id")
    pkgs_dupe = [
        {"pkg_id": "PKG_A", "tm_id": "06_01", "desi": 100},  # GECERLI
        {"pkg_id": "PKG_A", "tm_id": "06_01", "desi": 200},  # Hata: duplikat
        {"pkg_id": "PKG_B", "tm_id": "07_01", "desi": 50},   # GECERLI
    ]
    clean3, errors3 = validate_packages(pkgs_dupe)
    _print_errors(errors3)
    _result_line("PKG_A (ilk) ve PKG_B gecmeli", len(clean3), len(errors3))
    assert len(clean3)  == 2
    assert len(errors3) == 1

    print(f"\n  [3d] MAX_SINGLE_DESI siniri = {MAX_SINGLE_DESI} desi")
    limit_pkg = [
        {"pkg_id": "P_MAX",  "tm_id": "34_01", "desi": MAX_SINGLE_DESI},
        {"pkg_id": "P_OVER", "tm_id": "34_01", "desi": MAX_SINGLE_DESI + 1},
    ]
    clean4, errors4 = validate_packages(limit_pkg)
    _print_errors(errors4)
    assert len(clean4)  == 1, f"Sadece P_MAX gecmeli, gelen={len(clean4)}"
    assert len(errors4) == 1
    print(f"  [OK]  {MAX_SINGLE_DESI} desi gecti, {MAX_SINGLE_DESI + 1} desi reddedildi")

    print("\n  [3e] Tamamen bos paket listesi")
    clean5, errors5 = validate_packages([])
    assert len(clean5) == 0 and len(errors5) == 0
    print("  [OK]  Bos liste hata firlatmadi")


# ── Runner ────────────────────────────────────────────────────────────────────

def run_stress_test():
    print("\n" + "=" * 60)
    print("  LogiAI — Veri Validasyon Stres Testi")
    print("  src.preprocessing.data_validation ::")
    print("    validate_transfer_centers, validate_vehicles, validate_packages")
    print("=" * 60)

    senaryolar = [
        ("Bozuk TM Verisi",    test_bozuk_tm_verisi),
        ("Bozuk Arac Verisi",  test_bozuk_arac_verisi),
        ("Bozuk Paket Verisi", test_bozuk_paket_verisi),
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
