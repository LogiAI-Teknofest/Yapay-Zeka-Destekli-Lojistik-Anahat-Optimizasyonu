import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from redis import Redis

from .config import get_redis_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ── Sabit Veri ────────────────────────────────────────────────────────────────

TM_MAX_CAP: Dict[str, int] = {
    "34_01": 5000,
    "06_01": 4500,
    "35_01": 3000,
    "07_01": 2500,
}

# α_i: Tır yanaşma uygunluğu — Kişi B'nin OR-Tools modeli bu bilgiyi kullanır
TM_ACCEPTS_TRUCK: Dict[str, bool] = {
    "34_01": True,
    "06_01": True,
    "35_01": False,
    "07_01": False,
}

VEHICLE_INFO: Dict[str, Dict] = {
    "TIR_01":        {"type": "Tır",         "capacity": 3000},
    "KAMYON_01":     {"type": "Kamyon",       "capacity": 1500},
    "HAF_KAMYON_01": {"type": "Hafif Kamyon", "capacity": 800},
    "KAMYONET_01":   {"type": "Kamyonet",     "capacity": 400},
}

# 2E-VRP echelon sabitleri (MVP'de sadece SECOND aktif)
ECHELON_FIRST  = "1"   # küçük araç → TM toplama
ECHELON_SECOND = "2"   # büyük araç → ana hedef


class RedisStateManager:
    def __init__(self):
        self.redis: Redis = get_redis_client()
        try:
            self.redis.ping()
        except Exception as exc:
            logger.error(f"Redis baglantisi kurulamadi: {exc}")
            raise

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

        now = datetime.now().isoformat()
        for tm_id, max_cap in TM_MAX_CAP.items():
            pipe.hset(
                f"TM:{tm_id}:State",
                mapping={
                    "MaxCapacity":    max_cap,
                    "CurrentLoad":    0,
                    "OverloadAmount": 0,   # δ_i — esnek kisit takibi
                    "AcceptsTruck":   int(TM_ACCEPTS_TRUCK.get(tm_id, False)),
                    "UpdatedAt":      now,
                },
            )
        for vehicle_id, info in VEHICLE_INFO.items():
            pipe.hset(
                f"Vehicle:{vehicle_id}:State",
                mapping={
                    "Type":        info["type"],
                    "MaxCapacity": info["capacity"],
                    "CurrentLoad": 0,
                    "Location":    "Depo",
                    "UpdatedAt":   now,
                },
            )
        pipe.execute()
        logger.info("Redis durumlari sifirlandi (secici).")

    # ── Read ─────────────────────────────────────────────────────────────────

    def get_tm_state(self, tm_id: str) -> Dict:
        raw = self.redis.hgetall(f"TM:{tm_id}:State")
        return {
            "tm_id":         tm_id,
            "max_cap":       int(float(raw.get("MaxCapacity",    TM_MAX_CAP.get(tm_id, 0)))),
            "current":       float(raw.get("CurrentLoad",   0)),
            "overload":      float(raw.get("OverloadAmount", 0)),   # δ_i
            "accepts_truck": raw.get("AcceptsTruck", "0") == "1",
            "updated_at":    raw.get("UpdatedAt", "-"),
        }

    def get_vehicle_state(self, vehicle_id: str) -> Dict:
        raw = self.redis.hgetall(f"Vehicle:{vehicle_id}:State")
        info = VEHICLE_INFO.get(vehicle_id, {"type": "Bilinmiyor", "capacity": 1})
        return {
            "vehicle_id": vehicle_id,
            "type":       raw.get("Type", info["type"]),
            "max_cap":    int(float(raw.get("MaxCapacity", info["capacity"]))),
            "current":    float(raw.get("CurrentLoad", 0)),
            "location":   raw.get("Location", "Depo"),
            "updated_at": raw.get("UpdatedAt", "-"),
        }

    def list_tm_states(self) -> List[Dict]:
        return [self.get_tm_state(tm_id) for tm_id in TM_MAX_CAP]

    def list_vehicle_states(self) -> List[Dict]:
        return [self.get_vehicle_state(v) for v in VEHICLE_INFO]

    def get_total_overload(self) -> float:
        """Tum TM'lerin toplam delta_i asim miktari."""
        return sum(self.get_tm_state(tm_id)["overload"] for tm_id in TM_MAX_CAP)

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

        tm_state      = self.get_tm_state(package["tm_id"])
        vehicle_state = self.get_vehicle_state(vehicle_id)

        # Arac kapasitesi — sert kisit
        if vehicle_state["current"] + desi > vehicle_state["max_cap"]:
            logger.info(
                f"Yuklenemedi: {vehicle_id} dolu "
                f"(gerekli={desi:.1f}, kalan={vehicle_state['max_cap'] - vehicle_state['current']:.1f})"
            )
            return False

        now = datetime.now().isoformat()
        pipe = self.redis.pipeline()
        pipe.hincrbyfloat(vehicle_key, "CurrentLoad", desi)
        pipe.hset(vehicle_key, "UpdatedAt", now)
        pipe.hincrbyfloat(tm_key, "CurrentLoad", desi)
        pipe.hset(tm_key, "UpdatedAt", now)

        # TM elleçleme — esnek kisit (delta_i)
        new_tm_load = tm_state["current"] + desi
        if new_tm_load > tm_state["max_cap"]:
            overload_delta = new_tm_load - tm_state["max_cap"]
            pipe.hincrbyfloat(tm_key, "OverloadAmount", overload_delta)

        pipe.execute()
        logger.info(
            f"Yuklendi: {package.get('pkg_id', '?')} -> {vehicle_id} / "
            f"TM:{package['tm_id']} ({desi:.1f} desi)"
        )
        return True

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
        self.redis.zadd(f"ETA:TM:{tm_id}",      {vehicle_id: eta_ts})
        self.redis.zadd(f"ETA:Vehicle:{vehicle_id}", {tm_id: eta_ts})
        logger.info(f"ETA guncellendi: {vehicle_id} -> TM:{tm_id} @ {eta_ts}")

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
        ts  = ts or datetime.now().strftime("%Y%m%d%H%M%S")
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
