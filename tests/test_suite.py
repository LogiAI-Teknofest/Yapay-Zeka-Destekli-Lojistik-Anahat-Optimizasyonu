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

import time
from datetime import datetime

import redis

_r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


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

        vehs = list(_r.scan_iter(match="Vehicle:*:State"))
        assert len(vehs) == 4, f"4 arac bekleniyor, gelen={len(vehs)}"

        cap = _r.hget("Vehicle:KIR_TIR_01:State", "MaxCapacity")
        assert cap == "22400", f"MaxCapacity=22400 bekleniyor, gelen={cap}"
        print(f"  OK  {len(vehs)} arac  KIR_TIR_01 MaxCap={cap}")
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

        pkg = {"pkg_id": "PKG_001", "tm_id": "İstanbul", "desi": 500.5}
        ok  = sm.try_load_package(pkg, "KIR_TIR_01")
        assert ok, "Yukleme basarili olmali"
        v_state = sm.get_vehicle_state("KIR_TIR_01")
        assert v_state["current"] == 500.5, f"Arac yuku=500.5 bekleniyor, gelen={v_state['current']}"
        print(f"  OK  500.5 desi yuklendi (float hincrbyfloat calisiyor)")

        big = {"pkg_id": "PKG_BIG", "tm_id": "İstanbul", "desi": 22400.0}
        ok2 = sm.try_load_package(big, "KIR_TIR_01")
        assert not ok2, "Kapasite asimi reddedilmeli"
        print(f"  OK  Arac kapasitesi asimi dogru reddedildi")

        pkg2 = {"pkg_id": "PKG_002", "tm_id": "İstanbul", "desi": 200.0}
        sm.try_load_package(pkg2, "KIR_TIR_01")
        v_state2 = sm.get_vehicle_state("KIR_TIR_01")
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

        _r.hset("TM:Yalova:State", mapping={"MaxCapacity": 883655, "CurrentLoad": 883200, "OverloadAmount": 0})

        pkg = {"pkg_id": "PKG_OVR", "tm_id": "Yalova", "desi": 600.0}
        ok  = sm.try_load_package(pkg, "KIR_HAFIF_01")
        assert ok, "Arac kapasitesi uygun, yuklenmeli (TM soft constraint)"

        tm_state = sm.get_tm_state("Yalova")
        assert tm_state["overload"] > 0, f"delta_i > 0 bekleniyor, gelen={tm_state['overload']}"
        print(f"  OK  TM:Yalova  delta_i={tm_state['overload']:.1f} desi")

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
        sm.update_eta("KIR_KAMYON_01", "Kocaeli", now + 1800)
        sm.update_eta("KIR_HAFIF_01",  "Kocaeli", now + 900)

        result = sm.get_next_vehicle_eta("Kocaeli")
        assert result is not None
        next_v, eta_ts = result
        assert next_v == "KIR_HAFIF_01", f"En yakin arac yanlis: {next_v}"
        print(f"  OK  En yakin arac: {next_v}  ETA={eta_ts}")

        etas = sm.get_all_etas_for_tm("Kocaeli")
        assert len(etas) == 2
        print(f"  OK  TM:Kocaeli icin {len(etas)} ETA kaydi")
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

        key = sm.log_route_echelon("KIR_TIR_01", ["İstanbul", "Yalova"], echelon=ECHELON_SECOND)
        assert "Echelon:2" in key
        stops = _r.lrange(key, 0, -1)
        assert stops == ["İstanbul", "Yalova"], f"Rota duraklari yanlis: {stops}"
        assert _r.ttl(key) > 0
        print(f"  OK  Echelon-2: {stops}  TTL={_r.ttl(key)}s")

        key1 = sm.log_route_echelon("KIR_KAMYONET_01", ["Kocaeli"], echelon=ECHELON_FIRST)
        assert "Echelon:1" in key1
        print(f"  OK  Echelon-1 iskelet hazir (MVP'de pasif)")

        routes = sm.get_route_echelon("KIR_TIR_01", echelon=ECHELON_SECOND)
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
        from tests.mock_generator import generate_tm_demand_items

        sm = RedisStateManager()
        items = generate_tm_demand_items(count=18, seed=42)

        route_packages = {
            "KIR_TIR_01":    [p for p in items if p["tm_id"] in ("İstanbul", "Yalova")][:5],
            "KIR_KAMYON_01": [p for p in items if p["tm_id"] in ("Kocaeli", "Tekirdağ")][:3],
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


# ── Test 8: Veri Yükleme Doğrulama ───────────────────────────────────────────
def test_data_validation():
    _section("TEST 8: Veri Yukleme Dogrulama (data_loader)")
    try:
        import json
        import os
        import tempfile
        from src.utils.data_loader import load_input, DataContractError

        # Gerçek JSON başarılı yüklenmeli
        data = load_input("data/raw/logiai_mvp_input.json")
        assert "distance_matrix" in data
        assert "rental_routes" in data
        assert "daily_demand" in data
        print(f"  OK  Gercek JSON yuklendi ({len(data['daily_demand'])} gun, "
              f"{len(data['distance_matrix'])} sehir)")

        # Eksik alan → DataContractError
        bad = {k: v for k, v in data.items() if k != "distance_matrix"}
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(bad, f)
        try:
            load_input(path)
            print("  FAIL  DataContractError bekleniyor")
            return FAIL
        except DataContractError:
            print("  OK  Eksik alan DataContractError fırlattı")
        finally:
            os.unlink(path)

        # Dosya yok → DataContractError
        try:
            load_input("var_olmayan_dosya_12345.json")
            return FAIL
        except DataContractError:
            print("  OK  Olmayan dosya DataContractError fırlattı")

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
        assert hasattr(config, "LOGIAI_API_URL")
        print(f"  OK  REDIS_HOST={config.REDIS_HOST}:{config.REDIS_PORT}")
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
    ]

    results = []
    for name, fn in tests:
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
