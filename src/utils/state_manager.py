import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import redis as _redis

from .config import REDIS_HOST, REDIS_PORT, REDIS_DB

logger = logging.getLogger(__name__)
_MAX_RETRIES = 3

# Modül düzeyinde paylaşılan tek ConnectionPool — her RedisStateManager()
# örneği aynı havuzu kullanır, her çağrıda yeni TCP soketi açılmaz.
_pool = _redis.ConnectionPool(
    host=REDIS_HOST,
    port=REDIS_PORT,
    db=REDIS_DB,
    decode_responses=True,
)

# 2E-VRP echelon sabitleri (MVP'de sadece SECOND aktif)
ECHELON_FIRST  = "1"
ECHELON_SECOND = "2"

# Bağlantı yalnızca süreç başına bir kez ping ile doğrulanır; her
# RedisStateManager() örneğinde tekrar round-trip yapılmaz.
_connection_verified = False


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RedisStateManager:
    def __init__(self, *, db: int = REDIS_DB):
        global _connection_verified
        if db == REDIS_DB:
            # Üretim yolu: paylaşılan pool + süreç-başına tek ping.
            self.redis: _redis.Redis = _redis.Redis(connection_pool=_pool)
            if not _connection_verified:
                try:
                    self.redis.ping()
                    _connection_verified = True
                except Exception as exc:
                    logger.error(f"Redis baglantisi kurulamadi: {exc}")
                    raise
        else:
            # Test / izole yol (ör. db=REDIS_TEST_DB=15): doğrudan bağlantı,
            # singleton ping uygulanmaz — üretim DB'sine dokunulmaz (#55).
            self.redis = _redis.Redis(
                host=REDIS_HOST, port=REDIS_PORT, db=db, decode_responses=True
            )

    # ── Reset ────────────────────────────────────────────────────────────────

    def reset_states(self) -> None:
        """Pattern bazli secici sifirlama — flushdb kullanmaz."""
        patterns = (
            "TM:*:State", "Vehicle:*:State",
            "ETA:TM:*", "ETA:Vehicle:*",
            "Route:*:Echelon:*",
        )
        pipe = self.redis.pipeline()
        for pat in patterns:
            for key in self.redis.scan_iter(match=pat):
                pipe.delete(key)

        pipe.execute()
        logger.info("Redis durumlari sifirlandi (secici). Arac durumları icin load_vehicle_state() cagirilmali.")

    def load_vehicle_state(self, data: dict) -> None:
        """
        rental_routes'taki araç örneklerini Redis'e yükler.
        Kapasite bilgisi vehicles_info'dan alınır; bu sayede VEHICLE_INFO
        hardcoded sabitine bağımlılık kalmaz (Kural 7 — SSoT).
        """
        vehicles_info = data.get("vehicles_info", {})
        pipe = self.redis.pipeline()
        now = _now_utc_iso()
        seen: set = set()
        for vehicles in data.get("rental_routes", {}).values():
            for v in vehicles:
                vid = v["id"]
                if vid in seen:
                    continue
                seen.add(vid)
                vtype = v.get("vehicle_type", "")
                capacity = (
                    vehicles_info.get(vtype, {}).get("capacity_desi")
                    or v.get("capacity_desi", 0)
                )
                pipe.hset(
                    f"Vehicle:{vid}:State",
                    mapping={
                        "Type":        vtype,
                        "MaxCapacity": capacity,
                        "CurrentLoad": 0,
                        "Location":    "Depo",
                        "UpdatedAt":   now,
                    },
                )
        pipe.execute()
        logger.info("Araç durumları JSON'dan yüklendi (%d araç).", len(seen))

    # ── Read ─────────────────────────────────────────────────────────────────

    def get_tm_state(self, tm_id: str) -> Dict:
        raw = self.redis.hgetall(f"TM:{tm_id}:State")
        return {
            "tm_id":         tm_id,
            "max_cap":       int(float(raw.get("MaxCapacity", 0))),
            "current":       float(raw.get("CurrentLoad",   0)),
            "overload":      float(raw.get("OverloadAmount", 0)),
            "accepts_truck": raw.get("AcceptsTruck", "0") == "1",
            "updated_at":    raw.get("UpdatedAt", "-"),
        }

    def get_vehicle_state(self, vehicle_id: str) -> Dict:
        raw = self.redis.hgetall(f"Vehicle:{vehicle_id}:State")
        return {
            "vehicle_id": vehicle_id,
            "type":       raw.get("Type", "Bilinmiyor"),
            "max_cap":    int(float(raw.get("MaxCapacity", 1))),
            "current":    float(raw.get("CurrentLoad", 0)),
            "location":   raw.get("Location", "Depo"),
            "updated_at": raw.get("UpdatedAt", "-"),
        }

    def list_tm_states(self) -> List[Dict]:
        tm_keys = list(self.redis.scan_iter(match="TM:*:State"))
        if not tm_keys:
            return []
        pipe = self.redis.pipeline()
        for key in tm_keys:
            pipe.hgetall(key)
        raws = pipe.execute()
        states = []
        for key, raw in zip(tm_keys, raws):
            tm_id = key.replace("TM:", "").replace(":State", "")
            states.append({
                "tm_id":         tm_id,
                "max_cap":       int(float(raw.get("MaxCapacity", 0))),
                "current":       float(raw.get("CurrentLoad", 0)),
                "overload":      float(raw.get("OverloadAmount", 0)),
                "accepts_truck": raw.get("AcceptsTruck", "0") == "1",
                "updated_at":    raw.get("UpdatedAt", "-"),
            })
        return states

    def list_vehicle_states(self) -> List[Dict]:
        vehicle_keys = list(self.redis.scan_iter(match="Vehicle:*:State"))
        if not vehicle_keys:
            return []
        pipe = self.redis.pipeline()
        for key in vehicle_keys:
            pipe.hgetall(key)
        raws = pipe.execute()
        states = []
        for key, raw in zip(vehicle_keys, raws):
            vehicle_id = key.replace("Vehicle:", "").replace(":State", "")
            states.append({
                "vehicle_id": vehicle_id,
                "type":       raw.get("Type", "Bilinmiyor"),
                "max_cap":    int(float(raw.get("MaxCapacity", 1))),
                "current":    float(raw.get("CurrentLoad", 0)),
                "location":   raw.get("Location", "Depo"),
                "updated_at": raw.get("UpdatedAt", "-"),
            })
        return states

    def get_total_overload(self) -> float:
        """Tum TM'lerin toplam delta_i asim miktari."""
        tm_keys = list(self.redis.scan_iter(match="TM:*:State"))
        if not tm_keys:
            return 0.0
        pipe = self.redis.pipeline()
        for key in tm_keys:
            pipe.hget(key, "OverloadAmount")
        total = 0.0
        for raw in pipe.execute():
            if raw:
                total += float(raw)
        return total

    # ── Load ─────────────────────────────────────────────────────────────────

    def try_load_package(self, package: Dict, vehicle_id: str) -> bool:
        """
        Araci kapasitesi varsa yukler (hincrbyfloat — float desi destegi).
        Arac kapasitesi: sert kisit — asim olursa reddeder.
        TM elleçleme kapasitesi: esnek kisit — asim olursa delta_i gunceller, yukler.
        """
        vehicle_key = f"Vehicle:{vehicle_id}:State"
        tm_key      = f"TM:{package['tm_id']}:State"
        desi        = float(package["desi"])
        for attempt in range(_MAX_RETRIES):
            try:
                with self.redis.pipeline() as pipe:
                    pipe.watch(vehicle_key, tm_key)
                    vehicle_raw = pipe.hgetall(vehicle_key)
                    tm_raw = pipe.hgetall(tm_key)

                    vehicle_state = {
                        "max_cap": int(float(vehicle_raw.get("MaxCapacity", 1))),
                        "current": float(vehicle_raw.get("CurrentLoad", 0)),
                    }
                    tm_state = {
                        "max_cap": int(float(tm_raw.get("MaxCapacity", 0))),
                        "current": float(tm_raw.get("CurrentLoad", 0)),
                    }

                    if vehicle_state["current"] + desi > vehicle_state["max_cap"]:
                        pipe.unwatch()
                        logger.info(
                            f"Yuklenemedi: {vehicle_id} dolu "
                            f"(gerekli={desi:.1f}, kalan={vehicle_state['max_cap'] - vehicle_state['current']:.1f})"
                        )
                        return False

                    now = _now_utc_iso()
                    new_tm_load = tm_state["current"] + desi
                    overload_amount = 0.0
                    if tm_state["max_cap"] > 0 and new_tm_load > tm_state["max_cap"]:
                        overload_amount = new_tm_load - tm_state["max_cap"]

                    pipe.multi()
                    pipe.hincrbyfloat(vehicle_key, "CurrentLoad", desi)
                    pipe.hset(vehicle_key, "UpdatedAt", now)
                    pipe.hincrbyfloat(tm_key, "CurrentLoad", desi)
                    pipe.hset(tm_key, mapping={
                        "UpdatedAt": now,
                        "OverloadAmount": overload_amount,
                    })
                    pipe.execute()
                    logger.info(
                        f"Yuklendi: {package.get('pkg_id', '?')} -> {vehicle_id} / "
                        f"TM:{package['tm_id']} ({desi:.1f} desi)"
                    )
                    return True
            except _redis.WatchError:
                logger.debug(
                    "try_load_package WatchError (attempt %d/%d) vehicle=%s tm=%s",
                    attempt + 1,
                    _MAX_RETRIES,
                    vehicle_id,
                    package["tm_id"],
                )
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(0.01 * (2 ** attempt))  # 10ms, 20ms — exponential backoff
                continue

        logger.warning(
            "try_load_package: max retries (%d) aşıldı vehicle=%s tm=%s",
            _MAX_RETRIES,
            vehicle_id,
            package["tm_id"],
        )
        return False

    def apply_route_loads(self, route_packages: Dict[str, List[Dict]]) -> Dict[str, List]:
        loaded, failed = [], []
        for vehicle_id, packages in route_packages.items():
            for package in packages:
                if self.try_load_package(package, vehicle_id):
                    loaded.append(package)
                else:
                    failed.append(package)
        return {"loaded": loaded, "failed": failed}

    # ── ETA Tracking ─────────────────────────────────────────────────────────

    def update_eta(self, vehicle_id: str, tm_id: str, eta_ts: int) -> None:
        """Arac -> TM tahmini varis zamanini ZSET'e yaz (score = unix timestamp)."""
        pipe = self.redis.pipeline()
        pipe.zadd(f"ETA:TM:{tm_id}", {vehicle_id: eta_ts})
        pipe.zadd(f"ETA:Vehicle:{vehicle_id}", {tm_id: eta_ts})
        pipe.execute()
        logger.info(f"ETA guncellendi: {vehicle_id} -> TM:{tm_id} @ {eta_ts}")

    def clear_vehicle_eta(self, vehicle_id: str) -> None:
        """Araç teslim tamamlandı: tüm TM ETA ZSET'lerinden kaldır (zombi önlemi, #55)."""
        tms = [tm for tm, _ in self.redis.zrange(
            f"ETA:Vehicle:{vehicle_id}", 0, -1, withscores=True
        )]
        pipe = self.redis.pipeline()
        for tm_id in tms:
            pipe.zrem(f"ETA:TM:{tm_id}", vehicle_id)
        pipe.delete(f"ETA:Vehicle:{vehicle_id}")
        pipe.execute()

    def get_next_vehicle_eta(self, tm_id: str) -> Optional[Tuple[str, int]]:
        """TM'ye en erken ulasacak arac ve ETA timestamp'ini dondur."""
        result = self.redis.zrange(f"ETA:TM:{tm_id}", 0, 0, withscores=True)
        if not result:
            return None
        vehicle_id, eta_ts = result[0]
        return vehicle_id, int(eta_ts)

    def get_all_etas_for_tm(self, tm_id: str) -> List[Tuple[str, int]]:
        """TM'ye yonelik tum araclarin ETA listesi (en yakindan uzaga)."""
        return [
            (v, int(ts))
            for v, ts in self.redis.zrange(f"ETA:TM:{tm_id}", 0, -1, withscores=True)
        ]

    # ── 2E-VRP Route Key Skeleton ─────────────────────────────────────────────

    def log_route_echelon(
        self,
        vehicle_id: str,
        route: List[str],
        echelon: str = ECHELON_SECOND,
        ts: Optional[str] = None,
    ) -> str:
        """
        Rota verisini Redis LIST'e yaz (24 saat TTL).
        Echelon 1 = kucuk arac -> TM toplama  (2E-VRP first leg, MVP'de pasif)
        Echelon 2 = buyuk arac -> ana hedef   (2E-VRP second leg, MVP'de aktif)
        """
        ts  = ts or datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        key = f"Route:{vehicle_id}:Echelon:{echelon}:{ts}"
        pipe = self.redis.pipeline()
        for stop in route:
            pipe.rpush(key, stop)
        pipe.expire(key, 86400)
        pipe.execute()
        logger.info(f"Rota kaydedildi: {key}  durak={route}")
        return key

    def get_route_echelon(self, vehicle_id: str, echelon: str = ECHELON_SECOND) -> List[List[str]]:
        """Bir araç için tüm echelon rotalarini döndür."""
        pattern = f"Route:{vehicle_id}:Echelon:{echelon}:*"
        routes = []
        for key in self.redis.scan_iter(match=pattern):
            routes.append(self.redis.lrange(key, 0, -1))
        return routes
