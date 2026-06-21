"""
LogiAI — MVP Test Suite (Kisi C)
state_manager, data_loader modüllerini test eder.
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

from src.utils.config import REDIS_HOST, REDIS_PORT, REDIS_TEST_DB

# Test izolasyon DB'si (varsayılan 15) — üretim DB'sine dokunulmaz (#55).
_r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_TEST_DB, decode_responses=True)

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


def _section(title: str):
    print(f"\n{'='*55}")
    print(f"  {title}")
    print("=" * 55)


_INPUT_JSON = "data/processed/logiai_mvp_input.json"


def _load_data():
    from src.utils.data_loader import load_input
    return load_input(_INPUT_JSON)


# ── SSoT yardımcıları (Kural 4): araç/şehir/kapasite JSON'dan türetilir ──────────
def _ssot_vehicle(data, idx: int = 0):
    """idx. kiralık aracın (id, tip, kapasite) üçlüsü — kapasite JSON'dan."""
    vehicles = [v for vs in data["rental_routes"].values() for v in vs]
    v = vehicles[idx]
    vtype = v.get("vehicle_type", "")
    cap = data["vehicles_info"].get(vtype, {}).get("capacity_desi") or v.get("capacity_desi", 0)
    return v["id"], vtype, float(cap)


def _ssot_city(data, idx: int = 0):
    return sorted(data["distance_matrix"].keys())[idx]


def _prepare_vehicle_states(sm):
    sm.load_vehicle_state(_load_data())


# ── Test 1: Redis ─────────────────────────────────────────────────────────────
def test_redis_connection():
    _section("TEST 1: Redis Baglantisi")
    try:
        _r.ping()
        info = _r.info("server")
        print(f"  OK  ping basarili  (Redis {info.get('redis_version', '?')})")
        return PASS
    except Exception as e:
        print(f"  SKIP  {e}")
        return SKIP


# ── Test 2: reset_states + load_vehicle_state ────────────────────────────────
def test_reset_states():
    _section("TEST 2: reset_states + load_vehicle_state (SSoT — JSON'dan yükleme)")
    try:
        from src.utils.state_manager import RedisStateManager
        sm = RedisStateManager(db=REDIS_TEST_DB)
        sm.reset_states()

        # reset_states artık Vehicle key oluşturmuyor; load_vehicle_state çağrılmalı
        data = _load_data()
        sm.load_vehicle_state(data)

        vehs = list(_r.scan_iter(match="Vehicle:*:State"))
        assert len(vehs) >= 1, f"En az 1 arac bekleniyor, gelen={len(vehs)}"

        # SSoT: beklenen kapasite JSON'dan türetilir (hardcode yok)
        vid, _, cap = _ssot_vehicle(data)
        stored = _r.hget(f"Vehicle:{vid}:State", "MaxCapacity")
        assert int(float(stored)) == int(cap), f"MaxCapacity={cap} bekleniyor, gelen={stored}"
        print(f"  OK  {len(vehs)} arac  {vid} MaxCap={stored}")
        return PASS
    except Exception as e:
        print(f"  FAIL  {e}")
        return FAIL


# ── Test 3: try_load_package ──────────────────────────────────────────────────
def test_load_package():
    _section("TEST 3: try_load_package (hincrbyfloat + sert kisit)")
    try:
        from src.utils.state_manager import RedisStateManager
        sm = RedisStateManager(db=REDIS_TEST_DB)
        data = _load_data()
        sm.load_vehicle_state(data)
        vid, _, cap = _ssot_vehicle(data)   # SSoT: arac id + kapasite JSON'dan
        city = _ssot_city(data)

        pkg = {"pkg_id": "PKG_001", "tm_id": city, "desi": 500.5}
        ok  = sm.try_load_package(pkg, vid)
        assert ok, "Yukleme basarili olmali"
        v_state = sm.get_vehicle_state(vid)
        assert v_state["current"] == 500.5, f"Arac yuku=500.5 bekleniyor, gelen={v_state['current']}"
        print(f"  OK  500.5 desi yuklendi (float hincrbyfloat calisiyor)")

        # kapasiteyi asan paket (kapasite JSON'dan) -> reddedilmeli
        big = {"pkg_id": "PKG_BIG", "tm_id": city, "desi": cap}
        ok2 = sm.try_load_package(big, vid)
        assert not ok2, "Kapasite asimi reddedilmeli"
        print(f"  OK  Arac kapasitesi asimi dogru reddedildi")

        pkg2 = {"pkg_id": "PKG_002", "tm_id": city, "desi": 200.0}
        sm.try_load_package(pkg2, vid)
        v_state2 = sm.get_vehicle_state(vid)
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
        sm = RedisStateManager(db=REDIS_TEST_DB)
        data = _load_data()
        sm.load_vehicle_state(data)
        vid, _, cap = _ssot_vehicle(data)
        city = _ssot_city(data, 1)   # farklı bir şehir

        # SSoT: TM kapasitesi JSON araç kapasitesinden türetilir (magic number yok).
        # TM tam dolulukta başlatılır; araç kapasitesi altında bir yük TM'yi taşırır.
        tm_cap = int(cap)
        _r.hset(f"TM:{city}:State", mapping={"MaxCapacity": tm_cap, "CurrentLoad": tm_cap, "OverloadAmount": 0})

        desi = max(1.0, cap * 0.05)   # araç sert kısıtını geçer, TM esnek kısıtını taşırır
        pkg = {"pkg_id": "PKG_OVR", "tm_id": city, "desi": desi}
        ok  = sm.try_load_package(pkg, vid)
        assert ok, "Arac kapasitesi uygun, yuklenmeli (TM soft constraint)"

        tm_state = sm.get_tm_state(city)
        assert tm_state["overload"] > 0, f"delta_i > 0 bekleniyor, gelen={tm_state['overload']}"
        print(f"  OK  TM:{city}  delta_i={tm_state['overload']:.1f} desi")

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
        sm = RedisStateManager(db=REDIS_TEST_DB)
        data = _load_data()
        v0, _, _ = _ssot_vehicle(data, 0)
        v1, _, _ = _ssot_vehicle(data, 1)
        city = _ssot_city(data)

        now = int(time.time())
        sm.update_eta(v0, city, now + 1800)
        sm.update_eta(v1, city, now + 900)

        result = sm.get_next_vehicle_eta(city)
        assert result is not None
        next_v, eta_ts = result
        assert next_v == v1, f"En yakin arac yanlis: {next_v}"   # 900 < 1800
        print(f"  OK  En yakin arac: {next_v}  ETA={eta_ts}")

        etas = sm.get_all_etas_for_tm(city)
        assert len(etas) == 2
        print(f"  OK  TM:{city} icin {len(etas)} ETA kaydi")

        # clear_vehicle_eta: zombi temizligi (#55.2) — v0 ikinci bir TM'ye de
        # ulasiyor; teslim tamamlaninca her iki TM ZSET'inden de silinmeli.
        city2 = _ssot_city(data, 1)
        sm.update_eta(v0, city2, now + 600)
        sm.clear_vehicle_eta(v0)
        ids_city  = [v for v, _ in sm.get_all_etas_for_tm(city)]
        ids_city2 = [v for v, _ in sm.get_all_etas_for_tm(city2)]
        assert v0 not in ids_city,  f"{v0} TM:{city} ETA'sindan silinmeliydi"
        assert v0 not in ids_city2, f"{v0} TM:{city2} ETA'sindan silinmeliydi"
        assert v1 in ids_city,      f"{v1} korunmaliydi (yanlis silindi)"
        assert _r.exists(f"ETA:Vehicle:{v0}") == 0, "ETA:Vehicle index temizlenmeliydi"
        print(f"  OK  clear_vehicle_eta: {v0} tum TM ZSET'lerinden silindi, {v1} korundu")
        return PASS
    except Exception as e:
        print(f"  FAIL  {e}")
        return FAIL


# ── Test 6: 2E-VRP Route Key ──────────────────────────────────────────────────
def test_route_echelon():
    _section("TEST 6: 2E-VRP Route Key Iskeleti")
    try:
        from src.utils.state_manager import RedisStateManager, ECHELON_FIRST, ECHELON_SECOND
        sm = RedisStateManager(db=REDIS_TEST_DB)
        data = _load_data()
        vid, _, _ = _ssot_vehicle(data, 0)
        vid2, _, _ = _ssot_vehicle(data, 1)
        c0, c1 = _ssot_city(data, 0), _ssot_city(data, 1)

        key = sm.log_route_echelon(vid, [c0, c1], echelon=ECHELON_SECOND)
        assert "Echelon:2" in key
        stops = _r.lrange(key, 0, -1)
        assert stops == [c0, c1], f"Rota duraklari yanlis: {stops}"
        assert _r.ttl(key) > 0
        print(f"  OK  Echelon-2: {stops}  TTL={_r.ttl(key)}s")

        key1 = sm.log_route_echelon(vid2, [c0], echelon=ECHELON_FIRST)
        assert "Echelon:1" in key1
        print(f"  OK  Echelon-1 iskelet hazir (MVP'de pasif)")

        routes = sm.get_route_echelon(vid, echelon=ECHELON_SECOND)
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

        sm = RedisStateManager(db=REDIS_TEST_DB)
        data = _load_data()
        sm.load_vehicle_state(data)

        # SSoT: şehirler ve araç id'leri JSON'dan türetilir
        cities = sorted(data["distance_matrix"].keys())
        items = generate_tm_demand_items(count=18, seed=42, tm_ids=cities)
        v0, _, _ = _ssot_vehicle(data, 0)
        v1, _, _ = _ssot_vehicle(data, 1)
        half = cities[: max(1, len(cities) // 2)]

        route_packages = {
            v0: [p for p in items if p["tm_id"] in half][:5],
            v1: [p for p in items if p["tm_id"] not in half][:3],
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
        data = load_input("data/processed/logiai_mvp_input.json")
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

    tests = [
        ("Redis Baglantisi",        test_redis_connection, True),
        ("reset_states",            test_reset_states, True),
        ("try_load_package",        test_load_package, True),
        ("Overload (delta_i)",      test_overload_tracking, True),
        ("ETA Tracking",            test_eta_tracking, True),
        ("2E-VRP Route Iskelet",    test_route_echelon, True),
        ("apply_route_loads",       test_apply_route_loads, True),
        ("Veri Dogrulama",          test_data_validation, False),
        ("Config Env Vars",         test_config, False),
    ]

    results = []
    redis_ok = None
    for name, fn, needs_redis in tests:
        if needs_redis and redis_ok is False:
            _section(f"TEST SKIP: {name}")
            print("  SKIP  Redis baglantisi yok; Redis bagimli test atlandi")
            results.append((name, SKIP))
            continue

        if needs_redis and name != "Redis Baglantisi":
            try:
                from src.utils.state_manager import RedisStateManager
                RedisStateManager(db=REDIS_TEST_DB).reset_states()
            except Exception:
                redis_ok = False
                _section(f"TEST SKIP: {name}")
                print("  SKIP  Redis baglantisi yok; Redis bagimli test atlandi")
                results.append((name, SKIP))
                continue
        try:
            status = fn()
        except Exception as e:
            status = FAIL
            print(f"  FAIL  beklenmedik hata: {e}")
        if name == "Redis Baglantisi":
            redis_ok = status == PASS
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
