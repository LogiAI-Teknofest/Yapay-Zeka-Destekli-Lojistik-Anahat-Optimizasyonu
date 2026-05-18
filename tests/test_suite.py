"""
LogiAI — MVP Test Suite (Kisi C)
state_manager, data_validation modüllerini test eder.
Proje kök dizininden çalıştır: python tests/test_suite.py
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
import time
from datetime import datetime

logging.disable(logging.CRITICAL)

from src.utils.config import get_redis_client

_r = get_redis_client()

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


def _reset():
    for pat in ("TM:*:State", "Vehicle:*:State", "ETA:TM:*", "ETA:Vehicle:*", "Route:*"):
        for k in _r.scan_iter(match=pat):
            _r.delete(k)


def _section(title: str):
    print(f"\n{'='*55}")
    print(f"  {title}")
    print("=" * 55)


# ── Test 1: Redis ─────────────────────────────────────────────────────────────
def test_redis_connection():
    _section("TEST 1: Redis Baglantisi")
    try:
        _r.ping()
        info = _r.info("server")
        print(f"  OK  ping basarili  (Redis {info.get('redis_version', '?')})")
        return PASS
    except Exception as e:
        print(f"  FAIL  {e}")
        return FAIL


# ── Test 2: reset_states ──────────────────────────────────────────────────────
def test_reset_states():
    _section("TEST 2: reset_states (Secici Redis Sifirlama)")
    try:
        from src.utils.state_manager import RedisStateManager
        sm = RedisStateManager()
        sm.reset_states()

        tms  = list(_r.scan_iter(match="TM:*:State"))
        vehs = list(_r.scan_iter(match="Vehicle:*:State"))
        assert len(tms)  == 4, f"4 TM bekleniyor, gelen={len(tms)}"
        assert len(vehs) == 4, f"4 arac bekleniyor, gelen={len(vehs)}"

        cap      = _r.hget("TM:34_01:State", "MaxCapacity")
        overload = _r.hget("TM:34_01:State", "OverloadAmount")
        truck    = _r.hget("TM:34_01:State", "AcceptsTruck")
        assert cap      == "5000", f"MaxCapacity=5000 bekleniyor, gelen={cap}"
        assert overload == "0",    f"OverloadAmount=0 bekleniyor, gelen={overload}"
        assert truck    == "1",    f"AcceptsTruck=1 bekleniyor (34_01 tir kabul), gelen={truck}"
        print(f"  OK  {len(tms)} TM, {len(vehs)} arac  MaxCap={cap}  delta_i={overload}  Tir={truck}")
        return PASS
    except Exception as e:
        print(f"  FAIL  {e}")
        return FAIL


# ── Test 3: try_load_package ──────────────────────────────────────────────────
def test_load_package():
    _section("TEST 3: try_load_package (hincrbyfloat + sert kisit)")
    try:
        from src.utils.state_manager import RedisStateManager
        sm = RedisStateManager()

        pkg = {"pkg_id": "PKG_001", "tm_id": "34_01", "desi": 500.5}
        ok  = sm.try_load_package(pkg, "TIR_01")
        assert ok, "Yukleme basarili olmali"
        v_state = sm.get_vehicle_state("TIR_01")
        assert v_state["current"] == 500.5, f"Arac yuku=500.5 bekleniyor, gelen={v_state['current']}"
        print(f"  OK  500.5 desi yuklendi (float hincrbyfloat calisiyor)")

        big = {"pkg_id": "PKG_BIG", "tm_id": "34_01", "desi": 3000.0}
        ok2 = sm.try_load_package(big, "TIR_01")
        assert not ok2, "Kapasite asimi reddedilmeli"
        print(f"  OK  Arac kapasitesi asimi dogru reddedildi")

        pkg2 = {"pkg_id": "PKG_002", "tm_id": "34_01", "desi": 200.0}
        sm.try_load_package(pkg2, "TIR_01")
        v_state2 = sm.get_vehicle_state("TIR_01")
        assert v_state2["current"] == 700.5, f"Kumulatif yuk=700.5 bekleniyor, gelen={v_state2['current']}"
        print(f"  OK  Kumulatif yuk dogru: {v_state2['current']} desi")
        return PASS
    except Exception as e:
        print(f"  FAIL  {e}")
        return FAIL


# ── Test 4: delta_i ───────────────────────────────────────────────────────────
def test_overload_tracking():
    _section("TEST 4: TM Overload Takibi (delta_i esnek kisit)")
    try:
        from src.utils.state_manager import RedisStateManager
        sm = RedisStateManager()

        _r.hset("TM:06_01:State", "CurrentLoad", 4200)

        pkg = {"pkg_id": "PKG_OVR", "tm_id": "06_01", "desi": 600.0}
        ok  = sm.try_load_package(pkg, "HAF_KAMYON_01")
        assert ok, "Arac kapasitesi uygun, yuklenmeli (TM soft constraint)"

        tm_state = sm.get_tm_state("06_01")
        assert tm_state["overload"] > 0, f"delta_i > 0 bekleniyor, gelen={tm_state['overload']}"
        print(f"  OK  TM:06_01  delta_i={tm_state['overload']:.1f} desi")

        total = sm.get_total_overload()
        assert total > 0
        print(f"  OK  Sistem toplam delta_i={total:.1f} desi")
        return PASS
    except Exception as e:
        print(f"  FAIL  {e}")
        return FAIL


# ── Test 5: ETA Tracking ──────────────────────────────────────────────────────
def test_eta_tracking():
    _section("TEST 5: ETA Takibi (ETA:TM:* ZSET)")
    try:
        from src.utils.state_manager import RedisStateManager
        sm = RedisStateManager()

        now = int(time.time())
        sm.update_eta("KAMYON_01",     "35_01", now + 1800)
        sm.update_eta("HAF_KAMYON_01", "35_01", now + 900)

        result = sm.get_next_vehicle_eta("35_01")
        assert result is not None
        next_v, eta_ts = result
        assert next_v == "HAF_KAMYON_01", f"En yakin arac yanlis: {next_v}"
        print(f"  OK  En yakin arac: {next_v}  ETA={eta_ts}")

        etas = sm.get_all_etas_for_tm("35_01")
        assert len(etas) == 2
        print(f"  OK  TM:35_01 icin {len(etas)} ETA kaydi")
        return PASS
    except Exception as e:
        print(f"  FAIL  {e}")
        return FAIL


# ── Test 6: 2E-VRP Route Key ──────────────────────────────────────────────────
def test_route_echelon():
    _section("TEST 6: 2E-VRP Route Key Iskeleti")
    try:
        from src.utils.state_manager import RedisStateManager, ECHELON_FIRST, ECHELON_SECOND
        sm = RedisStateManager()

        key = sm.log_route_echelon("TIR_01", ["34_01", "06_01"], echelon=ECHELON_SECOND)
        assert "Echelon:2" in key
        stops = _r.lrange(key, 0, -1)
        assert stops == ["34_01", "06_01"], f"Rota duraklari yanlis: {stops}"
        assert _r.ttl(key) > 0
        print(f"  OK  Echelon-2: {stops}  TTL={_r.ttl(key)}s")

        key1 = sm.log_route_echelon("KAMYONET_01", ["07_01"], echelon=ECHELON_FIRST)
        assert "Echelon:1" in key1
        print(f"  OK  Echelon-1 iskelet hazir (MVP'de pasif)")

        routes = sm.get_route_echelon("TIR_01", echelon=ECHELON_SECOND)
        assert len(routes) >= 1
        print(f"  OK  get_route_echelon: {len(routes)} rota okundu")
        return PASS
    except Exception as e:
        print(f"  FAIL  {e}")
        return FAIL


# ── Test 7: apply_route_loads ─────────────────────────────────────────────────
def test_apply_route_loads():
    _section("TEST 7: apply_route_loads (State Manager yukleme entegrasyonu)")
    try:
        from src.utils.state_manager import RedisStateManager
        from tests.mock_generator import generate_test_packages

        sm = RedisStateManager()
        packages = generate_test_packages(count=18, seed=42)

        route_packages = {
            "TIR_01":    [p for p in packages if p["tm_id"] in ("34_01", "06_01")][:5],
            "KAMYON_01": [p for p in packages if p["tm_id"] in ("35_01", "07_01")][:3],
        }

        assigned = sm.apply_route_loads(route_packages)
        loaded = assigned["loaded"]
        failed = assigned["failed"]
        assert len(loaded) > 0, "Hic paket yuklenmedi"
        print(f"  OK  {len(loaded)} yuklendi, {len(failed)} reddedildi")
        return PASS
    except Exception as e:
        print(f"  FAIL  {e}")
        return FAIL


# ── Test 8: Veri Dogrulama ────────────────────────────────────────────────────
def test_data_validation():
    _section("TEST 8: Veri Dogrulama (data_validation)")
    try:
        import pandas as pd
        from src.preprocessing.data_validation import (
            validate_transfer_centers,
            validate_vehicles,
            validate_packages,
        )

        tm_df = pd.DataFrame({
            "TM_ID":    ["34_01", "07_01", "9_9",  "34_01"],
            "Capacity": [5000,    2500,    3000,    -100],
        })
        clean_tm, errors_tm = validate_transfer_centers(tm_df)
        assert len(clean_tm) == 2, f"2 gecerli TM bekleniyor, gelen={len(clean_tm)}"
        assert len(errors_tm) >= 2
        print(f"  OK  TM: {len(clean_tm)} gecerli, {len(errors_tm)} hata")

        v_df = pd.DataFrame({
            "Vehicle_ID": ["V001", "V002", "V001"],
            "Type":       ["Tir",  "UcakTipi", "Kamyon"],
            "Capacity":   [3000,   1500,        800],
        })
        clean_v, errors_v = validate_vehicles(v_df)
        assert len(errors_v) >= 2
        print(f"  OK  Arac: {len(clean_v)} gecerli, {len(errors_v)} hata")

        pkgs = [
            {"pkg_id": "P001", "tm_id": "34_01", "desi": 100},
            {"pkg_id": "P001", "tm_id": "34_01", "desi": 50},
            {"pkg_id": "P002", "tm_id": "99_99", "desi": 200},
            {"pkg_id": "P003", "tm_id": "06_01", "desi": -10},
            {"pkg_id": "P004", "tm_id": "07_01", "desi": 80},
        ]
        clean_pkgs, errors_pkgs = validate_packages(pkgs)
        assert len(clean_pkgs) == 2, f"2 gecerli paket bekleniyor, gelen={len(clean_pkgs)}"
        assert len(errors_pkgs) >= 3
        print(f"  OK  Paket: {len(clean_pkgs)} gecerli, {len(errors_pkgs)} hata")
        return PASS
    except Exception as e:
        print(f"  FAIL  {e}")
        return FAIL


# ── Test 9: Config ────────────────────────────────────────────────────────────
def test_config():
    _section("TEST 9: Config & Env Vars")
    try:
        from src.utils import config
        assert hasattr(config, "REDIS_HOST")
        assert hasattr(config, "REDIS_PORT")
        assert hasattr(config, "get_redis_client")
        print(f"  OK  REDIS_HOST={config.REDIS_HOST}:{config.REDIS_PORT}")
        return PASS
    except Exception as e:
        print(f"  FAIL  {e}")
        return FAIL


# ── Test 10: Data Ingestion ───────────────────────────────────────────────────
def test_data_ingestion():
    _section("TEST 10: Data Ingestion (data_ingestion modulu import)")
    try:
        from src.preprocessing.data_ingestion import (
            validate_transfer_centers,
            validate_vehicles,
            load_transfer_centers_to_redis,
        )
        import pandas as pd

        df = pd.DataFrame([
            {"TM_ID": "34_01", "Capacity": 5000},
            {"TM_ID": "BAD",   "Capacity": -1},
        ])
        clean, errors = validate_transfer_centers(df)
        assert len(clean) == 1
        print(f"  OK  data_ingestion import ve validasyon calisiyor ({len(clean)} gecerli)")
        return PASS
    except Exception as e:
        print(f"  FAIL  {e}")
        return FAIL


# ── Runner ────────────────────────────────────────────────────────────────────
def run_all_tests():
    print("\n" + "=" * 55)
    print("  LogiAI — MVP Test Suite (Kisi C)")
    print("=" * 55)
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    _reset()
    from src.utils.state_manager import RedisStateManager
    RedisStateManager().reset_states()

    tests = [
        ("Redis Baglantisi",        test_redis_connection),
        ("reset_states",            test_reset_states),
        ("try_load_package",        test_load_package),
        ("Overload (delta_i)",      test_overload_tracking),
        ("ETA Tracking",            test_eta_tracking),
        ("2E-VRP Route Iskelet",    test_route_echelon),
        ("apply_route_loads",       test_apply_route_loads),
        ("Veri Dogrulama",          test_data_validation),
        ("Config Env Vars",         test_config),
        ("Data Ingestion",          test_data_ingestion),
    ]

    results = []
    for name, fn in tests:
        _reset()
        try:
            from src.utils.state_manager import RedisStateManager
            RedisStateManager().reset_states()
        except Exception:
            pass
        try:
            status = fn()
        except Exception as e:
            status = FAIL
            print(f"  FAIL  beklenmedik hata: {e}")
        results.append((name, status))

    print("\n" + "=" * 55)
    print("  SONUCLAR")
    print("=" * 55)
    for name, status in results:
        marker = "PASS" if status == PASS else ("SKIP" if status == SKIP else "FAIL")
        print(f"  [{marker}]  {name}")

    passed = sum(1 for _, s in results if s == PASS)
    skipped = sum(1 for _, s in results if s == SKIP)
    print(f"\n  {passed}/{len(results)} test gecti" + (f"  ({skipped} atlandi)" if skipped else ""))
    print("=" * 55)
    return passed + skipped == len(results)


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
