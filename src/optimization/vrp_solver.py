"""
optimization/vrp_solver.py
==========================
Aşama 2 — OR-Tools Open VRP Çözücüsü + Fallback Mekanizması

SOLID — Single Responsibility:
    Yalnızca spot araç rotalama ve fallback ataması yapar.
    Kiralık atama, I/O veya domain modeli buraya girmez.

SOLID — Open/Closed:
    Yeni araç tipleri eklemek için yalnızca veri sözleşmesi güncellenir;
    bu modülün iç mantığı değişmez.

SOLID — Dependency Inversion:
    SpotVRPSolver sınıfı, somut OR-Tools nesnelerine değil,
    kendi _build_* metodlarından dönen soyut veri yapılarına bağımlıdır.
    Bu, birim testlerinde mock ile kolayca değiştirilebilmesini sağlar.

Mimari seçimler:
    - Tüm OR-Tools kurulumu bir sınıf içinde toplanmıştır;
      global durum yoktur (thread-safe).
    - _SCALE sabiti float → int dönüşümü için kullanılır (OR-Tools int ister).
    - Fallback, OR-Tools dışında tamamen bağımsız çalışır;
      çözücü arızasından etkilenmez.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from models.data_types import RouteKey, SpotAssignment

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Modül Sabitleri
# ─────────────────────────────────────────────────────────────────────────────

_SCALE: int          = 100        # float maliyet → OR-Tools int dönüşüm çarpanı
_TIME_LIMIT_SEC: int = 540        # 9 dakika
_INFEASIBLE_COST: int = 10_000_000  # izin verilmeyen yol cezası
_UNASSIGNED_PENALTY: int = 50_000_000  # atanmamış düğüm disjunction cezası
_DEPOT: int          = 0          # sanal depot node indeksi


# ─────────────────────────────────────────────────────────────────────────────
# Maliyet Yardımcıları  (private, modül düzeyinde saf fonksiyonlar)
# ─────────────────────────────────────────────────────────────────────────────

def _safe_spot_cost(
    cost_matrix: dict,
    origin: str,
    destination: str,
    vehicle_type: str,
) -> float:
    """
    Spot maliyetini güvenli biçimde döndürür.
    Kayıt yoksa float('inf') döner; bu güzergâh seçilmez.
    """
    try:
        return float(cost_matrix[origin][destination][vehicle_type]["spot"])
    except (KeyError, TypeError):
        return float("inf")


def _cheapest_spot_vehicle(
    cost_matrix: dict,
    spot_capacities: dict[str, float],
    origin: str,
    destination: str,
    remaining_desi: float,
) -> tuple[str, float, float] | None:
    """
    Kalan desi için toplam maliyeti en düşük spot araç tipini seçer.

    Değerlendirme kriteri: (araç sayısı × birim maliyet) / desi
    → desi başına normalize edilmiş maliyet minimizasyonu.

    Returns
    -------
    (vehicle_type, unit_cost, capacity_desi) veya None
    """
    best: tuple[str, float, float] | None = None
    best_cost_per_desi = float("inf")

    for vtype, cap in spot_capacities.items():
        unit_cost = _safe_spot_cost(cost_matrix, origin, destination, vtype)
        if unit_cost == float("inf"):
            continue
        n_vehicles     = math.ceil(remaining_desi / cap)
        cost_per_desi  = (n_vehicles * unit_cost) / remaining_desi
        if cost_per_desi < best_cost_per_desi:
            best_cost_per_desi = cost_per_desi
            best = (vtype, unit_cost, cap)

    return best


# ─────────────────────────────────────────────────────────────────────────────
# OR-Tools VRP Çözücü Sınıfı
# ─────────────────────────────────────────────────────────────────────────────

class SpotVRPSolver:
    """
    Spill talepler için OR-Tools tabanlı Open VRP çözücüsü.

    Her `solve()` çağrısı bağımsız bir OR-Tools modeli oluşturur;
    nesne yeniden kullanılabilir ve thread-safe'dir.

    Parameters
    ----------
    data : dict
        Veri sözleşmesine uygun Python sözlüğü.
    time_limit_sec : int
        Çözücü zaman sınırı (saniye). Varsayılan: 540 (9 dakika).
    """

    def __init__(
        self,
        data: dict[str, Any],
        time_limit_sec: int = _TIME_LIMIT_SEC,
    ) -> None:
        self._cost_matrix    = data["cost_matrix"]
        self._spot_caps      = data["spot_capacities"]
        self._time_limit     = time_limit_sec

    # ── Herkese Açık API ─────────────────────────────────────────────────────

    def solve(
        self,
        spill_demand: dict[RouteKey, float],
    ) -> list[SpotAssignment]:
        """
        Spill talepleri için spot araç ataması yapar.

        OR-Tools 9 dakikada çözüm üretemez ya da bazı düğümleri
        atanmamış bırakırsa, fallback mekanizması devreye girer.

        Parameters
        ----------
        spill_demand : dict[RouteKey, float]
            {(origin, dest): desi} biçiminde kalan talepler.

        Returns
        -------
        list[SpotAssignment]
        """
        if not spill_demand:
            log.info("Spill talep yok; Aşama 2 atlandı.")
            return []

        # Talepleri indekslenebilir listeye çevir
        demands: list[tuple[str, str, float]] = [
            (o, d, desi) for (o, d), desi in spill_demand.items()
        ]

        n_nodes    = len(demands) + 1           # +1 sanal depot
        fleet      = self._build_fleet(n_nodes)
        n_vehicles = len(fleet)
        cost_int   = self._build_cost_matrix(demands, n_nodes, fleet)
        node_cap   = self._build_node_demands(demands)

        log.info(
            "OR-Tools çözücü başlatılıyor: %d talep düğümü, %d araç…",
            len(demands), n_vehicles,
        )

        # Open VRP garantisi:
        # starts/ends listesi kullanılarak OR-Tools'un her araç için
        # 'depoya dön' kısıtını zorunlu kılması engellenir.
        # Depot → düğüm ve düğüm → depot maliyetleri cost_matrix'te 0
        # olduğundan araçlar son varış noktalarında serbestçe biter.
        starts = [_DEPOT] * n_vehicles
        ends   = [_DEPOT] * n_vehicles
        manager = pywrapcp.RoutingIndexManager(n_nodes, n_vehicles, starts, ends)
        routing = pywrapcp.RoutingModel(manager)

        self._register_cost(routing, manager, cost_int)
        self._register_capacity(routing, manager, node_cap, fleet)
        self._add_disjunctions(routing, manager, n_nodes)

        solution   = routing.SolveWithParameters(self._search_params())
        vrp_result = self._extract_solution(solution, routing, manager, demands, fleet)
        unassigned = self._find_unassigned(solution, routing, manager, n_nodes)

        # Fallback devreye giriyor mu?
        fallback_result = self._run_fallback(unassigned, demands)

        return vrp_result + fallback_result

    # ── Çözücü Kurulumu (private) ─────────────────────────────────────────────

    def _build_fleet(self, n_nodes: int) -> list[dict]:
        """
        Her araç tipinden (n_nodes - 1) adet araç içeren filo listesi üretir.

        Üst sınır: en kötü senaryoda her talep ayrı araç gerektirir.
        OR-Tools kullanılmayan araçları maliyet baskısıyla eler.
        """
        max_per_type = max(1, n_nodes - 1)
        fleet: list[dict] = []
        for vtype, cap in self._spot_caps.items():
            for _ in range(max_per_type):
                fleet.append({"type": vtype, "capacity": int(cap)})
        return fleet

    def _build_cost_matrix(
        self,
        demands: list[tuple[str, str, float]],
        n_nodes: int,
        fleet: list[dict],
    ) -> list[list[int]]:
        """
        OR-Tools için int maliyet matrisi üretir.

        Matris boyutu: n_nodes × n_nodes.
        En ucuz araç tipinin maliyeti baz alınır;
        gerçek araç seçimi çözüm sonrası ayrıca yapılır.
        """
        # En ucuz araç tipini belirle (toplam matris maliyeti üzerinden)
        def matrix_total(vtype: str) -> float:
            total = 0.0
            for i in range(1, n_nodes):
                for j in range(1, n_nodes):
                    if i != j:
                        o = demands[i - 1][0]
                        d = demands[j - 1][1]
                        c = _safe_spot_cost(self._cost_matrix, o, d, vtype)
                        total += c if c != float("inf") else _INFEASIBLE_COST
            return total

        cheapest_type = min(self._spot_caps.keys(), key=matrix_total)

        def arc_cost(fi: int, ti: int) -> int:
            if fi == _DEPOT or ti == _DEPOT:
                return 0
            o = demands[fi - 1][0]
            d = demands[ti - 1][1]
            if o == d:
                return 0
            c = _safe_spot_cost(self._cost_matrix, o, d, cheapest_type)
            return _INFEASIBLE_COST if c == float("inf") else int(c * _SCALE)

        return [[arc_cost(i, j) for j in range(n_nodes)] for i in range(n_nodes)]

    @staticmethod
    def _build_node_demands(
        demands: list[tuple[str, str, float]],
    ) -> list[int]:
        """Her düğümün talep miktarını (int desi) döndürür; depot = 0."""
        return [0] + [int(math.ceil(d[2])) for d in demands]

    def _register_cost(
        self,
        routing: pywrapcp.RoutingModel,
        manager: pywrapcp.RoutingIndexManager,
        cost_matrix: list[list[int]],
    ) -> None:
        """Maliyet callback'ini kayıt eder ve tüm araçlara atar."""

        def cost_cb(from_idx: int, to_idx: int) -> int:
            return cost_matrix[manager.IndexToNode(from_idx)][manager.IndexToNode(to_idx)]

        cb_idx = routing.RegisterTransitCallback(cost_cb)
        routing.SetArcCostEvaluatorOfAllVehicles(cb_idx)

    def _register_capacity(
        self,
        routing: pywrapcp.RoutingModel,
        manager: pywrapcp.RoutingIndexManager,
        node_demands: list[int],
        fleet: list[dict],
    ) -> None:
        """Kapasite boyutunu kayıt eder; her araca kendi kapasitesini atar."""

        def demand_cb(from_idx: int) -> int:
            return node_demands[manager.IndexToNode(from_idx)]

        demand_idx = routing.RegisterUnaryTransitCallback(demand_cb)

        vehicle_capacities = [int(v["capacity"]) for v in fleet]
        routing.AddDimensionWithVehicleCapacity(
            demand_idx,
            0,                    # slack yok
            vehicle_capacities,
            True,                 # kümülatif başlangıç = 0
            "Capacity",
        )

    @staticmethod
    def _add_disjunctions(
        routing: pywrapcp.RoutingModel,
        manager: pywrapcp.RoutingIndexManager,
        n_nodes: int,
    ) -> None:
        """
        Her talep düğümü için disjunction ekler.

        Ceza yüksek tutulur → çözücü her düğümü atamak ister.
        Atanmamış kalırsalar fallback devreye girer.
        """
        for node in range(1, n_nodes):
            routing.AddDisjunction(
                [manager.NodeToIndex(node)],
                _UNASSIGNED_PENALTY,
            )

    def _search_params(self) -> pywrapcp.DefaultRoutingSearchParameters:
        """Arama parametrelerini yapılandırır."""
        params = pywrapcp.DefaultRoutingSearchParameters()
        params.first_solution_strategy = (
            routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        )
        params.local_search_metaheuristic = (
            routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        )
        params.time_limit.seconds = self._time_limit
        params.log_search = False
        return params

    # ── Sonuç Çözümleme (private) ─────────────────────────────────────────────

    def _extract_solution(
        self,
        solution: Any,
        routing: pywrapcp.RoutingModel,
        manager: pywrapcp.RoutingIndexManager,
        demands: list[tuple[str, str, float]],
        fleet: list[dict],
    ) -> list[SpotAssignment]:
        """OR-Tools çözümünü SpotAssignment listesine çevirir."""
        if not solution:
            log.warning("OR-Tools çözüm üretemedi; VRP sonucu boş.")
            return []

        assignments: list[SpotAssignment] = []

        for vid in range(len(fleet)):
            if not routing.IsVehicleUsed(solution, vid):
                continue

            vtype   = fleet[vid]["type"]
            vcap    = float(fleet[vid]["capacity"])
            index   = routing.Start(vid)
            nodes:  list[int] = []

            while not routing.IsEnd(index):
                node = manager.IndexToNode(index)
                if node != _DEPOT:
                    nodes.append(node)
                index = solution.Value(routing.NextVar(index))

            if not nodes:
                continue

            # Güzergâh yolu ve maliyet hesabı
            route_path: list[str] = []
            total_desi  = 0.0
            total_cost  = 0.0

            for n in nodes:
                o, d, desi = demands[n - 1]
                total_desi += desi
                unit_c = _safe_spot_cost(self._cost_matrix, o, d, vtype)
                total_cost += 0.0 if unit_c == float("inf") else unit_c
                if not route_path:
                    route_path.append(o)
                route_path.append(d)

            assignments.append(
                SpotAssignment(
                    vehicle_type  = vtype,
                    origin        = demands[nodes[0] - 1][0],
                    destination   = demands[nodes[-1] - 1][1],
                    assigned_desi = total_desi,
                    capacity_desi = vcap,
                    cost          = total_cost,
                    route_path    = tuple(route_path),
                    source        = "vrp",
                )
            )

        log.info("OR-Tools %d spot atama üretti.", len(assignments))
        return assignments

    @staticmethod
    def _find_unassigned(
        solution: Any,
        routing: pywrapcp.RoutingModel,
        manager: pywrapcp.RoutingIndexManager,
        n_nodes: int,
    ) -> list[int]:
        """
        Çözümde atanmamış düğümlerin indekslerini döndürür.
        OR-Tools çözüm üretemezse tüm talep düğümleri atanmamış sayılır.
        """
        if not solution:
            return list(range(1, n_nodes))

        unassigned: list[int] = []
        for node in range(1, n_nodes):
            idx = manager.NodeToIndex(node)
            if routing.IsStart(idx) or solution.Value(routing.NextVar(idx)) == idx:
                unassigned.append(node)
        return unassigned

    # ── Fallback Mekanizması (private) ────────────────────────────────────────

    def _run_fallback(
        self,
        unassigned_nodes: list[int],
        demands: list[tuple[str, str, float]],
    ) -> list[SpotAssignment]:
        """
        Atanmamış düğümleri en ucuz spot araçla doğrudan eşler.

        Her düğüm için:
            1. Maliyet başına en verimli araç tipini seç.
            2. Kaç araç gerekiyorsa o kadar SpotAssignment üret.
        """
        if not unassigned_nodes:
            return []

        log.warning(
            "%d düğüm atanamamadı. Fallback devreye giriyor…",
            len(unassigned_nodes),
        )

        fallback: list[SpotAssignment] = []

        for node_idx in unassigned_nodes:
            origin, dest, desi = demands[node_idx - 1]

            best = _cheapest_spot_vehicle(
                self._cost_matrix,
                self._spot_caps,
                origin,
                dest,
                desi,
            )

            if best is None:
                log.error(
                    "Fallback: %s → %s için uygun spot araç bulunamadı! "
                    "Bu talep atanamadı.",
                    origin, dest,
                )
                continue

            vtype, unit_cost, vcap = best
            n_vehicles = math.ceil(desi / vcap)

            for batch_no in range(n_vehicles):
                batch_desi = min(vcap, desi - batch_no * vcap)
                fallback.append(
                    SpotAssignment(
                        vehicle_type  = vtype,
                        origin        = origin,
                        destination   = dest,
                        assigned_desi = batch_desi,
                        capacity_desi = vcap,
                        cost          = unit_cost,
                        route_path    = (origin, dest),
                        source        = "fallback",
                    )
                )

            log.info(
                "Fallback atama  %s → %s  |  %d × %s  (%.1f desi toplam)",
                origin, dest, n_vehicles, vtype, desi,
            )

        return fallback


# ─────────────────────────────────────────────────────────────────────────────
# Modül Düzeyinde Kolaylık Fonksiyonu
# ─────────────────────────────────────────────────────────────────────────────

def run_spot_vrp(
    data: dict[str, Any],
    spill_demand: dict[RouteKey, float],
    time_limit_sec: int = _TIME_LIMIT_SEC,
) -> list[SpotAssignment]:
    """
    SpotVRPSolver nesnesini oluşturup `solve()` çağrısı yapar.

    main.py ve boru hattı `SpotVRPSolver`'a doğrudan bağımlı olmak yerine
    bu fonksiyonu kullanır; iç implementasyon değişikliklerinden izole kalır.

    Parameters
    ----------
    data : dict
        Veri sözleşmesine uygun Python sözlüğü.
    spill_demand : dict[RouteKey, float]
        Aşama 1'den dönen atanmamış talepler.
    time_limit_sec : int
        OR-Tools zaman sınırı (saniye).

    Returns
    -------
    list[SpotAssignment]
    """
    solver = SpotVRPSolver(data, time_limit_sec=time_limit_sec)
    return solver.solve(spill_demand)
