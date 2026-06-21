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

Uygulanan 10 Kritik Mimari/Matematiksel Düzeltme
────────────────────────────────────────────────
Fix-01  Float Desi → Integer Scaling
        AddDimension yalnızca int alır. Desi değerleri _DESI_SCALE = 10
        ile tam sayıya çevrilir; raporlamada geri bölünür.

Fix-02  Node Splitting
        En büyük spot araç kapasitesini aşan talep düğümleri, VRP'ye
        girmeden önce o kapasite boyutunda alt-düğümlere bölünür.

Fix-03  Dinamik Araç Havuzu
        num_vehicles = ceil(toplam_desi / min_kapasite) + _SAFETY_MARGIN
        Statik "n_nodes - 1" yerine talebe dayalı dinamik formül.

Fix-04  Koşullu Alt Sınır
        Lower bound yalnızca aktif araçlara (depot→depot geçişi
        yapmayan) uygulanır; boş araçlar kısıt ihlali yaratmaz.

Fix-05  Garbage Collector Koruması
        Tüm Python callback referansları self._gc_refs listesinde
        tutulur; OR-Tools C++ katmanı callback'i çağırırken Python
        tarafı GC tarafından silinmez.

Fix-06  %10 Alt Kapasite Kısıtı
        Şartname gereği: aktif spot aracın yükü >= kapasite × 0.10.
        Fix-04 mekanizmasıyla birleştirilerek koşullu uygulanır.

Fix-07  TIR Yanaşma Sert Kısıtı
        data["tir_yanasma"][şehir] = False olan düğümlere TIR
        araçlarının atanması VehicleVar.RemoveValues ile engellenir.

Fix-08  Origin-İzole VRP Döngüleri
        run_spot_vrp() spill taleplerini origin bazında gruplar.
        Her origin için bağımsız SpotVRPSolver örneği çalıştırılır;
        araçlar kendi origin'ından kalkan taleplere karışamaz.

Fix-09  Homojen Filo Körlüğü ve Maliyet Duplikasyonu
        Her araç tipi için ayrı transit_callback fabrikası.
        Arc maliyet = SADECE KM (spot) maliyeti.
        Sabit kontak açma bedeli = SetFixedCostOfVehicle() ile eklenir.

Fix-10  Disjunction ve Sahte Maliyet Raporlaması
        Disjunction cezası = o güzergâhın max spot maliyeti × katsayı
        (sabit büyük sayi yerine). _extract_solution içinde
        routing.ObjectiveValue() KULLANILMAZ; rota adım adım
        dolaşılır, yalnızca fiziksel (KM + sabit) maliyet toplanır.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from models.data_types import RouteKey, SpotAssignment

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Modül Sabitleri
# ─────────────────────────────────────────────────────────────────────────────

# Fix-01: OR-Tools integer scaling sabitleri
_DESI_SCALE: int        = 10          # float desi → OR-Tools int çarpanı
_COST_SCALE: int        = 100         # float maliyet TL → OR-Tools int çarpanı

# Fix-03: Dinamik araç havuzu güvenlik payı
_SAFETY_MARGIN: int     = 2

# Fix-06: Spot araç minimum doluluk oranı
_MIN_LOAD_RATIO: float  = 0.10

# Fix-10: Disjunction ceza katsayısı
# penalty = max_route_cost × bu katsayı → solver her zaman ziyareti tercih eder
_DISJUNCTION_PENALTY_FACTOR: int = 10

_TIME_LIMIT_SEC: int    = 540         # 9 dakika
_INFEASIBLE_COST: int   = 10_000_000  # izin verilmeyen yol cezası
_DEPOT: int             = 0           # sanal depot düğüm indeksi


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
    spot_caps: dict[str, float],
    origin: str,
    destination: str,
    remaining_desi: float,
) -> tuple[str, float, float] | None:
    """
    Kalan desi için desi başına toplam maliyeti en düşük spot araç tipini seçer.

    Returns
    -------
    (vehicle_type, unit_cost, capacity_desi) veya None
    """
    best: tuple[str, float, float] | None = None
    if remaining_desi <= 0:
        return best
    best_cost_per_desi = float("inf")

    for vtype, cap in spot_caps.items():
        unit_cost = _safe_spot_cost(cost_matrix, origin, destination, vtype)
        if unit_cost == float("inf"):
            continue
        n_vehicles    = math.ceil(remaining_desi / cap)
        cost_per_desi = (n_vehicles * unit_cost) / remaining_desi
        if cost_per_desi < best_cost_per_desi:
            best_cost_per_desi = cost_per_desi
            best = (vtype, unit_cost, cap)

    return best


# ─────────────────────────────────────────────────────────────────────────────
# OR-Tools VRP Çözücü Sınıfı
# ─────────────────────────────────────────────────────────────────────────────

class SpotVRPSolver:
    """
    TEK BİR origin noktasına ait spill talepleri için OR-Tools tabanlı
    Open VRP çözücüsü.

    Fix-08: Her origin için bağımsız örnek oluşturulur.
    Fix-05: Tüm Python callback referansları self._gc_refs'te tutulur;
            C++ katmanı callback'e işaret ederken GC silemez.

    Parameters
    ----------
    data : dict
        Veri sözleşmesine uygun Python sözlüğü.
        Beklenen anahtarlar:
          - cost_matrix         (zorunlu)
          - spot_capacities     (zorunlu)  {vtype: desi}
          - tir_yanasma         (opsiyonel) {şehir: bool}  Fix-07
          - vehicle_fixed_costs (opsiyonel) {vtype: TL}    Fix-09
    time_limit_sec : int
        Çözücü zaman sınırı (saniye). Varsayılan: 540.
    """

    def __init__(
        self,
        data: dict[str, Any],
        time_limit_sec: int = _TIME_LIMIT_SEC,
    ) -> None:
        self._cost_matrix: dict          = data["cost_matrix"]
        self._spot_caps: dict[str, float] = data["spot_capacities"]

        # Fix-07: TIR yanaşma kuralları — {şehir: True/False}
        # Varsayılan True (TIR girebilir); yalnızca False olanlar kısıtlanır.
        self._tir_yanasma: dict[str, bool] = data.get("tir_yanasma", {})

        # Fix-09: Araç başına sabit kontak/açılış bedeli — {vtype: TL}
        self._fixed_costs: dict[str, float] = data.get("vehicle_fixed_costs", {})

        self._time_limit = time_limit_sec

        # Fix-05: GC koruması için referans deposu.
        # OR-Tools C++ katmanı Python nesnesine işaret ederken GC silebilir.
        # Tüm callback fonksiyonları ve OR-Tools değişken nesneleri buraya eklenir.
        self._gc_refs: list = []

    # ── Herkese Açık API ─────────────────────────────────────────────────────

    def solve(
        self,
        demands: list[tuple[str, str, float]],
    ) -> list[SpotAssignment]:
        """
        Tek bir origin'e ait spill talepleri için spot araç ataması yapar.

        Parameters
        ----------
        demands : list[(origin, dest, desi)]
            Aynı origin noktasından kalkan spill talepleri.

        Returns
        -------
        list[SpotAssignment]
        """
        if not demands:
            return []

        # Fix-02: Kapasite aşan büyük düğümleri böl
        demands = self._split_oversized(demands)

        n_nodes    = len(demands) + 1   # +1 sanal depot
        # Fix-03: Araç havuzunu talebe göre dinamik oluştur
        fleet      = self._build_fleet(demands)
        n_vehicles = len(fleet)
        # Fix-01: Desi değerlerini _DESI_SCALE ile tam sayıya çevir
        node_cap   = self._build_node_demands(demands)

        log.info(
            "OR-Tools başlatılıyor: origin=%s  |  %d düğüm, %d araç",
            demands[0][0], len(demands), n_vehicles,
        )

        starts  = [_DEPOT] * n_vehicles
        ends    = [_DEPOT] * n_vehicles
        manager = pywrapcp.RoutingIndexManager(n_nodes, n_vehicles, starts, ends)
        routing = pywrapcp.RoutingModel(manager)

        # Fix-09: Her araç tipi için ayrı transit_callback
        self._register_costs_per_type(routing, manager, demands, fleet)
        # Fix-01 + Fix-04 + Fix-06: Kapasite boyutu (tam sayı, koşullu LB)
        self._register_capacity(routing, manager, node_cap, fleet)
        # Fix-07: TIR yanaşma sert kısıtı
        self._apply_tir_constraints(routing, manager, demands, fleet)
        # Fix-10: Disjunction cezası > o düğümün max maliyeti
        self._add_disjunctions(routing, manager, demands, n_nodes)

        solution   = routing.SolveWithParameters(self._search_params())
        vrp_result = self._extract_solution(solution, routing, manager, demands, fleet)
        unassigned = self._find_unassigned(solution, routing, manager, n_nodes)
        fallback   = self._run_fallback(unassigned, demands)

        return vrp_result + fallback

    # ── Fix-02: Node Splitting ────────────────────────────────────────────────

    def _split_oversized(
        self,
        demands: list[tuple[str, str, float]],
    ) -> list[tuple[str, str, float]]:
        """
        Herhangi bir spot araç kapasitesini aşan talepleri,
        en büyük araç kapasitesi boyutunda alt-düğümlere böler.

        Kapasite aşan düğüm VRP'de hiçbir araca sığamaz → her zaman
        fallback'e düşer. Bölme ile bu kaçınılmaz fallback önlenir.
        """
        if not self._spot_caps:
            return demands

        max_cap = max(float(c) for c in self._spot_caps.values())
        result: list[tuple[str, str, float]] = []

        for o, d, desi in demands:
            if desi <= max_cap + 1e-6:
                result.append((o, d, desi))
                continue

            remaining   = float(desi)
            chunk_count = 0
            while remaining > 1e-6:
                chunk = min(remaining, max_cap)
                result.append((o, d, chunk))
                remaining   -= chunk
                chunk_count += 1

            log.info(
                "Fix-02: %s→%s  %.1f desi  → %d alt-düğüme bölündü.",
                o, d, desi, chunk_count,
            )

        return result

    # ── Fix-03: Dinamik Araç Havuzu ──────────────────────────────────────────

    def _build_fleet(
        self,
        demands: list[tuple[str, str, float]],
    ) -> list[dict]:
        """
        Fix-03: Araç havuzu boyutunu talebe göre dinamik hesaplar.

        Formül: ceil(toplam_desi / min_kapasite) + _SAFETY_MARGIN

        Araç tipleri round-robin olarak dağıtılır; her tipten en az 1 araç
        eklenerek araç tipi çeşitliliği korunur.

        Eski uygulama: max_per_type = n_nodes - 1 → çok sayıda gereksiz araç
        ve OR-Tools'ta ciddi ölçekleme sorunu.
        """
        total_desi = sum(d[2] for d in demands)
        min_cap    = min(float(c) for c in self._spot_caps.values())

        # Fix-03: Dinamik araç sayısı
        num_vehicles_total = math.ceil(total_desi / min_cap) + _SAFETY_MARGIN

        types     = list(self._spot_caps.items())   # [(vtype, cap), ...]
        num_types = len(types)
        per_type  = max(1, math.ceil(num_vehicles_total / num_types))

        fleet: list[dict] = []
        for vtype, cap in types:
            for _ in range(per_type):
                fleet.append({"type": vtype, "capacity": float(cap)})

        log.debug(
            "Fix-03: %d araç  (toplam_desi=%.1f, min_cap=%.1f, per_type=%d)",
            len(fleet), total_desi, min_cap, per_type,
        )
        return fleet

    # ── Fix-01: Düğüm Talep Değerlerini Tam Sayıya Çevirme ──────────────────

    @staticmethod
    def _build_node_demands(
        demands: list[tuple[str, str, float]],
    ) -> list[int]:
        """
        Fix-01: Desi değerlerini _DESI_SCALE ile tam sayıya çevirir.

        Eski uygulama: int(math.ceil(d[2])) → 5.3 desi için 6 desi tahsis
        ederek kapasite boyutunu yanlış hesaplatıyordu.
        Yeni: round(d[2] * 10) → 5.3 → 53 (çıkışta /10 ile geri alınır).
        """
        return [0] + [round(d[2] * _DESI_SCALE) for d in demands]

    # ── Fix-09: Araç Tipine Özel Maliyet Callback ─────────────────────────────

    def _register_costs_per_type(
        self,
        routing: pywrapcp.RoutingModel,
        manager: pywrapcp.RoutingIndexManager,
        demands: list[tuple[str, str, float]],
        fleet: list[dict],
    ) -> None:
        """
        Fix-09: Her araç tipi için ayrı transit_callback fabrikası.

        Arc maliyet = SADECE KM (spot) maliyeti.
        Sabit kontak açma bedeli ayrıca SetFixedCostOfVehicle ile eklenir.
        Böylece arc maliyeti ile sabit maliyet OR-Tools modelinde ayrışır;
        kullanılmayan araçlar yanlışlıkla sabit maliyete mahkûm olmaz.

        Eski uygulama: tek bir cost_cb tüm araçlara uygulanıyor,
        en ucuz tip baz alınıyordu → araç tipi fiyat farkı gözetilmiyordu.

        Fix-05: Her closure self._gc_refs'e eklenerek GC'den korunur.
        """
        # Fabrika fonksiyonu: döngü içi closure'da vtype'ın doğru
        # kapsamlanması için zorunlu (Python closure/loop tuzağı).
        def _make_km_cb(vtype: str):
            def km_cb(from_idx: int, to_idx: int) -> int:
                fi = manager.IndexToNode(from_idx)
                ti = manager.IndexToNode(to_idx)
                
                # Issue 45 Fix: Dönüş maliyeti (Open VRP) her zaman 0'dır
                if ti == _DEPOT:
                    return 0
                
                # Issue 45 Fix: Depodan (Origin) ilk çıkış bedava DEĞİLDİR
                if fi == _DEPOT:
                    o = demands[ti - 1][0]  # Ortak Origin
                    d = demands[ti - 1][1]  # İlk Varış noktası
                else:
                    o = demands[fi - 1][1]  # Bir önceki teslimatın varış noktası
                    d = demands[ti - 1][1]  # Şimdiki teslimatın varış noktası
                    
                if o == d:
                    return 0
                
                c = _safe_spot_cost(self._cost_matrix, o, d, vtype)
                return _INFEASIBLE_COST if c == float("inf") else int(c * _COST_SCALE)
            return km_cb

        for v_idx, veh in enumerate(fleet):
            vtype = veh["type"]
            cb    = _make_km_cb(vtype)
            self._gc_refs.append(cb)   # Fix-05: GC koruması

            cb_idx = routing.RegisterTransitCallback(cb)
            # Fix-09: Homojen filo körlüğü düzeltmesi —
            # SetArcCostEvaluatorOfVehicle her araca kendi callback'ini atar.
            routing.SetArcCostEvaluatorOfVehicle(cb_idx, v_idx)

            # Fix-09: Sabit kontak/kira bedeli arc maliyetinden ayrı tanımlanır.
            # OR-Tools bu bedeli yalnızca araç kullanıldığında ekler.
            fixed_tl = self._fixed_costs.get(vtype, 0.0)
            routing.SetFixedCostOfVehicle(round(fixed_tl * _COST_SCALE), v_idx)

    # ── Fix-01 + Fix-04 + Fix-06: Kapasite Boyutu ────────────────────────────

    def _register_capacity(
        self,
        routing: pywrapcp.RoutingModel,
        manager: pywrapcp.RoutingIndexManager,
        node_demands: list[int],
        fleet: list[dict],
    ) -> None:
        """
        Fix-01: Kapasite değerleri _DESI_SCALE ile tam sayıya çevrilir.
                AddDimensionWithVehicleCapacity yalnızca int listesi kabul eder.

        Fix-04: Alt sınır yalnızca aktif araçlara uygulanır.
                is_unused = 1 → araç depot→depot geçişi yapıyor (kullanılmıyor).
                Koşullu kısıt: cap_at_end + is_unused × min_load >= min_load
                  is_unused=1 → cap_at_end >= 0  (kısıtsız, trivial)
                  is_unused=0 → cap_at_end >= min_load  (aktif araç, LB uygulanır)

        Fix-06: Şartname gereği aktif spot araç yükü >= kapasite × %10.
        """
        def demand_cb(from_idx: int) -> int:
            return node_demands[manager.IndexToNode(from_idx)]

        self._gc_refs.append(demand_cb)   # Fix-05: GC koruması
        demand_idx = routing.RegisterUnaryTransitCallback(demand_cb)

        # Fix-01: Kapasite tam sayı (_DESI_SCALE ile ölçekli)
        vehicle_caps = [
            round(veh["capacity"] * _DESI_SCALE) for veh in fleet
        ]

        routing.AddDimensionWithVehicleCapacity(
            demand_idx,
            0,             # slack yok
            vehicle_caps,  # Fix-01: per-vehicle int kapasite listesi
            True,          # kümülatif başlangıç = 0
            "Capacity",
        )

        cap_dim   = routing.GetDimensionOrDie("Capacity")
        solver_cp = routing.solver()

        for v_idx, veh in enumerate(fleet):
            cap_scaled = vehicle_caps[v_idx]
            # Fix-06: %10 alt limit (tam sayı; _DESI_SCALE uygulandı)
            min_load   = round(cap_scaled * _MIN_LOAD_RATIO)

            end_node      = routing.End(v_idx)
            start_node    = routing.Start(v_idx)
            cap_at_end    = cap_dim.CumulVar(end_node)
            next_of_start = routing.NextVar(start_node)

            # Fix-04: is_unused — 1 eğer araç depot'tan doğrudan depot'a gidiyor
            # solver_cp.IsEqualCstVar(IntVar, int) → {0,1} değerli IntVar döner
            is_unused = solver_cp.IsEqualCstVar(next_of_start, end_node)
            self._gc_refs.append(is_unused)   # Fix-05: GC koruması

            # Fix-04 + Fix-06: Koşullu alt sınır kısıtı
            # Yeniden düzenleme: cap_at_end + is_unused × min_load >= min_load
            # Eşdeğer:          cap_at_end >= min_load × (1 − is_unused)
            solver_cp.Add(
                cap_at_end + is_unused * min_load >= min_load
            )

    # ── Fix-07: TIR Yanaşma Sert Kısıtı ─────────────────────────────────────

    def _apply_tir_constraints(
        self,
        routing: pywrapcp.RoutingModel,
        manager: pywrapcp.RoutingIndexManager,
        demands: list[tuple[str, str, float]],
        fleet: list[dict],
    ) -> None:
        """
        Fix-07: tir_yanasma[şehir] = False olan hedef şehirlere
        TIR araçlarının girmesini VehicleVar.RemoveValues ile engeller.

        Sert kısıt (hard constraint) olduğu için disjunction veya
        penalty kullanılmaz; TIR adayı tamamen kaldırılır.
        """
        tir_indices = [
            v for v, veh in enumerate(fleet) if veh["type"] == "Tır"
        ]
        if not tir_indices:
            return   # Filoda TIR yoksa kısıt uygulanmaz

        for node_idx, (_, dest, _) in enumerate(demands):
            # Varsayılan True (TIR girebilir); yalnızca açıkça False olanlar kısıtlanır
            if not self._tir_yanasma.get(dest, True):
                routing_idx = manager.NodeToIndex(node_idx + 1)
                routing.VehicleVar(routing_idx).RemoveValues(tir_indices)
                log.debug(
                    "Fix-07: '%s' → TIR yanaşma yasak; %d TIR aracı hariç tutuldu.",
                    dest, len(tir_indices),
                )

    # ── Fix-10: Disjunction — Dinamik Ceza ───────────────────────────────────

    def _add_disjunctions(
        self,
        routing: pywrapcp.RoutingModel,
        manager: pywrapcp.RoutingIndexManager,
        demands: list[tuple[str, str, float]],
        n_nodes: int,
    ) -> None:
        """
        Fix-10: Her talep düğümü için disjunction ekler.

        Ceza = o güzergâhın tüm araç tiplerindeki max spot maliyeti
               × _DISJUNCTION_PENALTY_FACTOR

        Eski uygulama: sabit _UNASSIGNED_PENALTY = 50_000_000 kullanıyordu.
        Bu değer bazı güzergâhlarda ziyaretten ucuza gelebilir (sahte ceza).
        Yeni uygulama: gerçek maliyet baz alındığından solver her zaman
        düğüme gitmeyi atlamaktan daha avantajlı görür.
        """
        for node in range(1, n_nodes):
            o, d, _ = demands[node - 1]

            # Bu güzergâhın tüm araç tipleri arasındaki maksimum spot maliyeti
            max_cost = 0.0
            for vtype in self._spot_caps:
                c = _safe_spot_cost(self._cost_matrix, o, d, vtype)
                if c != float("inf") and c > max_cost:
                    max_cost = c

            # Maliyet bulunamazsa büyük sabit değer kullan
            penalty = (
                round(max_cost * _COST_SCALE * _DISJUNCTION_PENALTY_FACTOR)
                or _INFEASIBLE_COST * _DISJUNCTION_PENALTY_FACTOR
            )

            routing.AddDisjunction([manager.NodeToIndex(node)], penalty)


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

    # ── Fix-10: Gerçek Maliyet Hesabı ────────────────────────────────────────

    def _extract_solution(
        self,
        solution: Any,
        routing: pywrapcp.RoutingModel,
        manager: pywrapcp.RoutingIndexManager,
        demands: list[tuple[str, str, float]],
        fleet: list[dict],
    ) -> list[SpotAssignment]:
        """
        Fix-10: routing.ObjectiveValue() KULLANILMAZ.

        OR-Tools objective'i disjunction cezalarını ve sanal maliyetleri
        içerir; bunlar raporlama için anlamsızdır.
        Rota adım adım dolaşılır; yalnızca fiziksel maliyet toplanır:
            fiziksel_maliyet = Σ arc_km_maliyeti + sabit_kontak_bedeli

        Fix-61: Çok duraklı rotalar artık lumped (toplu) değil, her bacak
        için ayrı SpotAssignment üretilir. Sabit kontak bedeli yalnızca
        ilk bacağa yazılır; ara bacaklar saf KM maliyetiyle raporlanır.
        """
        if not solution:
            log.warning("OR-Tools çözüm üretemedi; VRP sonucu boş.")
            return []

        assignments: list[SpotAssignment] = []

        for vid in range(len(fleet)):
            if not routing.IsVehicleUsed(solution, vid):
                continue

            vtype = fleet[vid]["type"]
            vcap  = float(fleet[vid]["capacity"])
            index = routing.Start(vid)
            nodes: list[int] = []

            while not routing.IsEnd(index):
                node = manager.IndexToNode(index)
                if node != _DEPOT:
                    nodes.append(node)
                index = solution.Value(routing.NextVar(index))

            if not nodes:
                continue

            # Fix-61: Rota tam düğüm listesini oluştur (depot dahil değil)
            # Her durak için ayrı SpotAssignment üretilir. Sabit kontak bedeli 
            # aracın ilk hareketinde bir kez ödenir.
            fixed_cost_remaining = self._fixed_costs.get(vtype, 0.0)
            
            route_origin = demands[nodes[0] - 1][0]
            current_location = route_origin

            for node_idx in nodes:
                req_origin, req_dest, req_desi = demands[node_idx - 1]
                
                c = _safe_spot_cost(self._cost_matrix, current_location, req_dest, vtype)
                # Issue 48 Fix: inf maliyeti bedava sanma, aşırı yüksek bir rakam yansıt
                arc_cost = c if c != float("inf") else 9999999.0
                
                leg_cost = round(fixed_cost_remaining + arc_cost, 2)
                fixed_cost_remaining = 0.0   # sonraki duraklar sabit bedel ödemez
                
                assignments.append(
                    SpotAssignment(
                        vehicle_type  = vtype,
                        origin        = req_origin,
                        destination   = req_dest,
                        assigned_desi = req_desi,
                        capacity_desi = vcap,
                        cost          = leg_cost,
                        route_path    = (current_location, req_dest),
                        source        = "vrp",
                    )
                )
                current_location = req_dest

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

    # ── Fallback Mekanizması ──────────────────────────────────────────────────

    def _run_fallback(
        self,
        unassigned_nodes: list[int],
        demands: list[tuple[str, str, float]],
    ) -> list[SpotAssignment]:
        """
        Atanmamış düğümleri en ucuz spot araçla doğrudan eşler.

        Her düğüm için:
            1. Fix-52: Seçilen araç kapasitesinin %10'unun altında kalan
               kargolar spot araca verilmez; unassigned olarak loglanır.
            2. Desi başına en verimli araç tipini seç.
            3. Kaç araç gerekiyorsa o kadar SpotAssignment üret.
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
                self._cost_matrix, self._spot_caps, origin, dest, desi,
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

                # Issue 47 Fix: Şartname Kuralı (Spot araç %10 doluluğa ulaşmazsa YOLA ÇIKAMAZ)
                # Bu yük sonraki güne devredilmeli / atanamayan olarak kalmalıdır.
                if batch_desi < vcap * _MIN_LOAD_RATIO:
                    log.warning(
                        "Fallback: %s → %s kapasitesinin %%%d altinda "
                        "(%.1f / %.1f). Yola CIKAMAZ! (Ertesi gune kalacak).",
                        origin, dest, int(_MIN_LOAD_RATIO * 100), batch_desi, vcap
                    )
                    continue

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
# Fix-08: Origin-İzole Kolaylık Fonksiyonu
# ─────────────────────────────────────────────────────────────────────────────

def run_spot_vrp(
    data: dict[str, Any],
    spill_demand: dict[RouteKey, float],
    time_limit_sec: int = _TIME_LIMIT_SEC,
) -> list[SpotAssignment]:
    """
    Fix-08: Spill talepleri origin bazında gruplanır; her origin için
    bağımsız bir SpotVRPSolver örneği oluşturulup çalıştırılır.

    Eski uygulama: tek bir SpotVRPSolver tüm spill talebini alıyordu.
    Bu, farklı origin'lerden kalkan araçların birbirinin düğümlerine
    geçiş yapmasına (fiziksel olarak imkânsız) izin veriyordu.

    Yeni uygulama: her origin → izole VRP → güvenilir rota kısıtı.

    Public API değişmedi: main.py ve boru hattı bu fonksiyonu kullanır.

    Parameters
    ----------
    data : dict
        Veri sözleşmesine uygun Python sözlüğü.
    spill_demand : dict[RouteKey, float]
        Aşama 1'den dönen {(origin, dest): desi} sözlüğü.
    time_limit_sec : int
        OR-Tools zaman sınırı (saniye); toplam bütçe origin sayısına
        dinamik olarak bölünerek her alt çözücüye aktarılır. (Fix-11)

    Returns
    -------
    list[SpotAssignment]
        Tüm origin'lerden toplanan spot atamalar.
    """
    if not spill_demand:
        log.info("Spill talep yok; Aşama 2 atlandı.")
        return []

    # Fix-08: Origin bazlı gruplama
    origin_groups: dict[str, list[tuple[str, str, float]]] = {}
    for (origin, dest), desi in spill_demand.items():
        origin_groups.setdefault(origin, []).append(
            (origin, dest, float(desi))
        )

    all_assignments: list[SpotAssignment] = []

    # Fix-11: Global başlangıç zamanı — her iterasyonda kalan bütçe
    # hesaplanarak o anki origin'e adil süre verilir.
    global_start  = time.monotonic()
    origins       = list(origin_groups.keys())
    total_origins = len(origins)

    for i, origin in enumerate(origins):
        elapsed        = time.monotonic() - global_start
        remaining_sec  = max(1, time_limit_sec - int(elapsed))
        remaining_orgs = total_origins - i          # bu + sonraki originler
        per_origin_sec = max(1, remaining_sec // remaining_orgs)

        log.info(
            "Fix-08+11: origin=%s  |  izole VRP başlatılıyor (%d talep)  "
            "|  süre_bütçesi=%ds  (kalan=%ds / %d origin).",
            origin, len(origins[i:i+1]), per_origin_sec, remaining_sec, remaining_orgs,
        )
        demands     = origin_groups[origin]
        solver      = SpotVRPSolver(data, time_limit_sec=per_origin_sec)
        assignments = solver.solve(demands)
        all_assignments.extend(assignments)

    log.info(
        "Aşama 2 tamamlandı: %d spot atama  |  %d origin grubu.",
        len(all_assignments), len(origin_groups),
    )
    return all_assignments
