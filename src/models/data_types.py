"""
models/data_types.py
====================
Proje genelinde kullanılan immutable veri yapıları (domain modelleri).

SOLID — Single Responsibility:
    Bu modül yalnızca veri sözleşmesini (data contract) tanımlar.
    İş mantığı, I/O ve çözücü kodu buraya girmez.

Tasarım notları:
    - frozen=True  → nesne oluşturulduktan sonra değiştirilemez; thread-safe.
    - eq=True      → unittest karşılaştırmaları için otomatik __eq__.
    - list alanlar frozen dataclass ile kullanılamaz; tuple kullanılır.
      (route_path gibi sıralı ama değişmez koleksiyonlar için tuple yeterlidir.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


# ─────────────────────────────────────────────────────────────────────────────
# Tip Takma Adları  (type aliases)
# ─────────────────────────────────────────────────────────────────────────────

# Güzergâh anahtarı: (kaynak şehir, hedef şehir)
RouteKey = tuple[str, str]

# Çözücü sonuç durumları
SolverStatus = Literal["OPTIMAL", "FEASIBLE", "FALLBACK", "NO_DEMAND"]

# Atama kaynağı
AssignmentSource = Literal["vrp", "fallback"]


# ─────────────────────────────────────────────────────────────────────────────
# Kiralık Araç Ataması
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RentalAssignment:
    """
    Tek bir kiralık araç için gerçekleştirilen kapasite atamasını temsil eder.

    Attributes
    ----------
    vehicle_id : str
        Araç kimliği (örn. "KIR_TIR_01").
    origin : str
        Kalkış şehri.
    destination : str
        Varış şehri.
    assigned_desi : float
        Bu araca yüklenen kargo miktarı (desi).
    capacity_desi : float
        Aracın toplam kapasitesi (desi).
    cost : float
        Araç için ödenen kiralık birim maliyet (TL).
    cost_type : str
        Maliyet türü; MVP'de her zaman "kiralik".
    """

    vehicle_id:     str
    origin:         str
    destination:    str
    assigned_desi:  float
    capacity_desi:  float
    cost:           float
    cost_type:      str = "kiralik"

    @property
    def utilisation_rate(self) -> float:
        """Araç doluluk oranı [0.0 – 1.0]."""
        if self.capacity_desi <= 0:
            return 0.0
        return min(self.assigned_desi / self.capacity_desi, 1.0)

    @property
    def route_key(self) -> RouteKey:
        return (self.origin, self.destination)


# ─────────────────────────────────────────────────────────────────────────────
# Spot Araç Ataması
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SpotAssignment:
    """
    Bir spot araç için gerçekleştirilen atamayı temsil eder.

    Attributes
    ----------
    vehicle_type : str
        Araç tipi ("Tır", "Kamyon", "Hafif Kamyon", "Kamyonet").
    origin : str
        Kalkış şehri.
    destination : str
        Varış şehri.
    assigned_desi : float
        Taşınan kargo miktarı (desi).
    capacity_desi : float
        Araç kapasitesi (desi).
    cost : float
        Bu güzergâh için ödenen spot maliyet (TL).
    route_path : tuple[str, ...]
        OR-Tools'dan dönen tam rota dizisi (örn. ("İstanbul", "Yalova")).
    source : AssignmentSource
        Atamanın kaynağı: "vrp" → OR-Tools çözdü, "fallback" → kaba atama.
    """

    vehicle_type:   str
    origin:         str
    destination:    str
    assigned_desi:  float
    capacity_desi:  float
    cost:           float
    route_path:     tuple[str, ...] = field(default_factory=tuple)
    source:         AssignmentSource = "vrp"

    @property
    def utilisation_rate(self) -> float:
        """Araç doluluk oranı [0.0 – 1.0]."""
        if self.capacity_desi <= 0:
            return 0.0
        return min(self.assigned_desi / self.capacity_desi, 1.0)

    @property
    def route_key(self) -> RouteKey:
        return (self.origin, self.destination)

    @property
    def is_fallback(self) -> bool:
        return self.source == "fallback"


# ─────────────────────────────────────────────────────────────────────────────
# Boru Hattı Sonucu
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PipelineResult:
    """
    Tüm optimizasyon boru hattının nihai çıktısı.

    Attributes
    ----------
    date : str
        Planlama tarihi (ISO 8601, örn. "2026-05-23").
    rental_assignments : tuple[RentalAssignment, ...]
        Aşama 1'den dönen kiralık araç atamaları.
    spot_assignments : tuple[SpotAssignment, ...]
        Aşama 2'den dönen spot araç atamaları.
    total_rental_cost : float
        Kiralık araç toplam maliyeti (TL).
    total_spot_cost : float
        Spot araç toplam maliyeti (TL).
    unassigned_demand : dict[RouteKey, float]
        Hiçbir araca atanamayan talep (desi). Boş dict → tam çözüm.
    solver_status : SolverStatus
        Çözücü sonuç kodu.
    """

    date:                str
    rental_assignments:  tuple[RentalAssignment, ...]
    spot_assignments:    tuple[SpotAssignment, ...]
    total_rental_cost:   float
    total_spot_cost:     float
    unassigned_demand:   dict[RouteKey, float]
    solver_status:       SolverStatus
    calisma_suresi_sn:   float = 0.0

    # ── Türetilmiş metrikler ─────────────────────────────────────────────────

    @property
    def total_cost(self) -> float:
        """Kiralık + spot toplam maliyet (TL)."""
        return self.total_rental_cost + self.total_spot_cost

    @property
    def has_unassigned(self) -> bool:
        return bool(self.unassigned_demand)

    @property
    def fallback_count(self) -> int:
        return sum(1 for a in self.spot_assignments if a.is_fallback)

    # ── Raporlama ────────────────────────────────────────────────────────────

    def summary(self) -> str:
        """İnsan tarafından okunabilir tek satırlık özet."""
        lines = [
            f"=== Linehaul Planı [{self.date}] ===",
            f"Çözücü durumu : {self.solver_status}",
            f"Kiralık atama : {len(self.rental_assignments):3d} araç  "
            f"→  {self.total_rental_cost:>12,.1f} TL",
            f"Spot atama    : {len(self.spot_assignments):3d} araç  "
            f"→  {self.total_spot_cost:>12,.1f} TL",
            f"Toplam maliyet:           {self.total_cost:>12,.1f} TL",
        ]
        if self.has_unassigned:
            lines.append("UYARI – Atanamayan talep:")
            for (o, d), desi in self.unassigned_demand.items():
                lines.append(f"  {o} → {d}: {desi:,.1f} desi")
        return "\n".join(lines)
