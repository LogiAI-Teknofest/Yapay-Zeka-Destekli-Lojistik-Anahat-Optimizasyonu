"""
main.py
=======
Ana Kontrolcü — Linehaul MVP Optimizasyon Boru Hattı

Görevleri:
    1. Komut satırı argümanlarını ayrıştır (CLI).
    2. JSON girdi dosyasını yükle ve doğrula.
    3. Her planlama tarihi için iki aşamalı boru hattını çalıştır.
    4. Sonuçları konsola ve isteğe bağlı olarak JSON çıktısına yaz.

SOLID — Single Responsibility:
    Bu dosya yalnızca uygulama akışını (orchestration) yönetir.
    İş mantığı, veri erişimi ve çözücü kodu buraya girmez.

Kullanım:
    # Tüm tarihler için çalıştır:
    python main.py --input logiai_mvp_input.json

    # Belirli bir tarih için çalıştır:
    python main.py --input logiai_mvp_input.json --date 2026-05-23

    # Sonuçları JSON dosyasına da yaz:
    python main.py --input logiai_mvp_input.json --output results.json

    # Zaman sınırını değiştir (saniye):
    python main.py --input logiai_mvp_input.json --time-limit 120
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import io
import json
import logging
import sys
from pathlib import Path
from typing import Any

# Windows terminalleri varsayılan olarak CP1254 kullanır; kutu çizgi
# karakterleri (═, ─) bu kodlamada tanımsız. stdout'u UTF-8'e zorla.
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf_8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── İç modüller ──────────────────────────────────────────────────────────────
from models.data_types import PipelineResult, RentalAssignment, SpotAssignment
from utils.data_loader import DataContractError, available_dates, load_input
from optimization.greedy import run_greedy_assignment
from optimization.vrp_solver import run_spot_vrp


def _run_pipeline_for_date(
    args_tuple: tuple[dict, str, int],
) -> PipelineResult:
    """
    ProcessPoolExecutor icin modul duzeyinde sarmalayici.
    Lokal fonksiyonlar pickle'lanamaz; bu fonksiyon modul seviyesinde
    tanimlandigi icin subprocess'lere guvenle aktarilabilir.
    """
    data, date, time_limit_sec = args_tuple
    return run_pipeline(data, date, time_limit_sec=time_limit_sec)

# ─────────────────────────────────────────────────────────────────────────────
# Loglama Kurulumu
# ─────────────────────────────────────────────────────────────────────────────

def _configure_logging(verbose: bool = False) -> None:
    """
    Uygulama genelinde loglama seviyesini yapılandırır.

    verbose=True → DEBUG (geliştirme)
    verbose=False → INFO (üretim)
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)-8s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

log = logging.getLogger("main")


# ─────────────────────────────────────────────────────────────────────────────
# Boru Hattı Orkestratörü
# ─────────────────────────────────────────────────────────────────────────────

def run_pipeline(
    data: dict[str, Any],
    date: str,
    time_limit_sec: int = 540,
) -> PipelineResult:
    """
    Tek bir tarih için iki aşamalı optimizasyon boru hattını çalıştırır.

    Aşama 1 → Greedy kiralık atama   (optimization.greedy)
    Aşama 2 → OR-Tools Open VRP      (optimization.vrp_solver)

    Parameters
    ----------
    data : dict
        load_input() tarafından doğrulanmış Python sözlüğü.
    date : str
        İşlenecek gün (ISO 8601, örn. "2026-05-23").
    time_limit_sec : int
        OR-Tools çözücüsü için saniye cinsinden zaman sınırı.

    Returns
    -------
    PipelineResult
    """
    log.info("══════ Boru hattı başlatıldı [%s] ══════", date)
    start_time = datetime.datetime.now()

    # ── Aşama 1: Greedy Kiralık Atama ────────────────────────────────────────
    rental_assignments_list, spill_demand = run_greedy_assignment(data, date)
    total_rental_cost = sum(a.cost for a in rental_assignments_list)

    # ── Aşama 2: Spot VRP + Fallback ─────────────────────────────────────────
    spot_assignments_list = run_spot_vrp(data, spill_demand, time_limit_sec)
    total_spot_cost = sum(a.cost for a in spot_assignments_list)

    # ── Çözücü Durum Kodu ────────────────────────────────────────────────────
    fallback_count = sum(1 for a in spot_assignments_list if a.is_fallback)

    if not spill_demand:
        solver_status = "NO_DEMAND"
    elif fallback_count > 0:
        solver_status = "FALLBACK"
    elif spot_assignments_list:
        solver_status = "FEASIBLE"
    else:
        solver_status = "OPTIMAL"

    # ── Atanamayan Talep Kontrolü ─────────────────────────────────────────────
    # Spill'e giren ama spot'a da atanamayan talepler (None dönen fallback)
    assigned_spill: dict[tuple[str, str], float] = {}
    for a in spot_assignments_list:
        key = (a.origin, a.destination)
        assigned_spill[key] = assigned_spill.get(key, 0.0) + a.assigned_desi

    unassigned: dict[tuple[str, str], float] = {}
    for (o, d), desi in spill_demand.items():
        leftover = desi - assigned_spill.get((o, d), 0.0)
        if leftover > 1.0:
            unassigned[(o, d)] = leftover

    elapsed = (datetime.datetime.now() - start_time).total_seconds()

    result = PipelineResult(
        date=date,
        rental_assignments=tuple(rental_assignments_list),
        spot_assignments=tuple(spot_assignments_list),
        total_rental_cost=total_rental_cost,
        total_spot_cost=total_spot_cost,
        unassigned_demand=unassigned,
        solver_status=solver_status,
        calisma_suresi_sn=round(elapsed, 3),
    )

    log.info("══════ Boru hattı tamamlandı [%s] ══════", date)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Konsol Çıktı Formatlayıcısı
# ─────────────────────────────────────────────────────────────────────────────

def _print_rental_table(assignments: tuple[RentalAssignment, ...]) -> None:
    if not assignments:
        print("  (kiralık atama yok)")
        return
    header = f"  {'Araç ID':<18} {'Güzergâh':<28} {'Atanan':>10} {'Kapasite':>10} {'Doluluk':>9} {'Maliyet':>12}"
    print(header)
    print("  " + "─" * (len(header) - 2))
    for a in assignments:
        route      = f"{a.origin} → {a.destination}"
        util_pct   = f"%{a.utilisation_rate * 100:5.1f}"
        cost_str   = f"{a.cost:>10,.0f} TL"
        print(
            f"  {a.vehicle_id:<18} {route:<28} "
            f"{a.assigned_desi:>9,.1f}d "
            f"{a.capacity_desi:>9,.0f}d "
            f"{util_pct:>9} "
            f"{cost_str:>12}"
        )


def _print_spot_table(assignments: tuple[SpotAssignment, ...]) -> None:
    if not assignments:
        print("  (spot atama yok)")
        return
    header = f"  {'Kaynak':<10} {'Tip':<15} {'Güzergâh':<28} {'Atanan':>10} {'Kapasite':>10} {'Maliyet':>12}"
    print(header)
    print("  " + "─" * (len(header) - 2))
    for a in assignments:
        route    = " → ".join(a.route_path) if a.route_path else f"{a.origin} → {a.destination}"
        src_tag  = f"[{a.source}]"
        cost_str = f"{a.cost:>10,.0f} TL"
        print(
            f"  {src_tag:<10} {a.vehicle_type:<15} {route:<28} "
            f"{a.assigned_desi:>9,.1f}d "
            f"{a.capacity_desi:>9,.0f}d "
            f"{cost_str:>12}"
        )


def print_result(result: PipelineResult) -> None:
    """Boru hattı sonucunu okunabilir tablo formatında konsola yazar."""
    SEP = "═" * 72

    print(f"\n{SEP}")
    print(result.summary())
    print(SEP)

    print("\n── Aşama 1 │ Kiralık Araç Atamaları ──────────────────────────────")
    _print_rental_table(result.rental_assignments)

    print("\n── Aşama 2 │ Spot Araç Atamaları ─────────────────────────────────")
    _print_spot_table(result.spot_assignments)

    if result.has_unassigned:
        print("\n⚠️  ATANAMAYAN TALEPLEr:")
        for (o, d), desi in result.unassigned_demand.items():
            print(f"   {o} → {d}: {desi:,.1f} desi")

    print(f"{SEP}\n")


# ─────────────────────────────────────────────────────────────────────────────
# JSON Çıktı Serileştirici
# ─────────────────────────────────────────────────────────────────────────────

def result_to_dict(result: PipelineResult) -> dict[str, Any]:
    """PipelineResult'ı JSON serileştirilebilir dict'e çevirir."""
    return {
        "date": result.date,
        "solver_status": result.solver_status,
        "total_rental_cost": result.total_rental_cost,
        "total_spot_cost": result.total_spot_cost,
        "total_cost": result.total_cost,
        "fallback_count": result.fallback_count,
        "unassigned_demand": {
            f"{o}_{d}": desi
            for (o, d), desi in result.unassigned_demand.items()
        },
        "calisma_suresi_sn": result.calisma_suresi_sn,
        "rental_assignments": [
            {
                "vehicle_id":    a.vehicle_id,
                "origin":        a.origin,
                "destination":   a.destination,
                "assigned_desi": a.assigned_desi,
                "capacity_desi": a.capacity_desi,
                "utilisation":   round(a.utilisation_rate, 4),
                "cost":          a.cost,
                "cost_type":     a.cost_type,
            }
            for a in result.rental_assignments
        ],
        "spot_assignments": [
            {
                "vehicle_type":  a.vehicle_type,
                "origin":        a.origin,
                "destination":   a.destination,
                "assigned_desi": a.assigned_desi,
                "capacity_desi": a.capacity_desi,
                "utilisation":   round(a.utilisation_rate, 4),
                "cost":          a.cost,
                "route_path":    list(a.route_path),
                "source":        a.source,
            }
            for a in result.spot_assignments
        ],
    }


def write_json_output(
    results: list[PipelineResult],
    output_path: str | Path,
) -> None:
    """Tüm tarih sonuçlarını tek bir JSON dosyasına yazar."""
    path = Path(output_path)
    payload = {
        "run_info": {
            "total_dates": len(results),
            "dates": [r.date for r in results],
            "grand_total_cost": sum(r.total_cost for r in results),
        },
        "results": [result_to_dict(r) for r in results],
    }
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    log.info("JSON çıktı yazıldı: %s", path.resolve())


# ─────────────────────────────────────────────────────────────────────────────
# CLI Arayüzü
# ─────────────────────────────────────────────────────────────────────────────

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="linehaul_optimizer",
        description=(
            "Linehaul MVP — 2 Aşamalı Kapasite Atama ve Rotalama Optimizasyonu\n"
            "Aşama 1: Greedy kiralık araç atama  |  Aşama 2: OR-Tools Open VRP"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--input", "-i",
        required=True,
        metavar="FILE",
        help="Girdi JSON dosyasının yolu (örn. logiai_mvp_input.json)",
    )
    parser.add_argument(
        "--date", "-d",
        metavar="YYYY-MM-DD",
        default=None,
        help=(
            "İşlenecek tarih. Belirtilmezse veri setindeki tüm tarihler işlenir."
        ),
    )
    parser.add_argument(
        "--output", "-o",
        metavar="FILE",
        default=None,
        help="Sonuçların yazılacağı JSON dosyası (isteğe bağlı).",
    )
    parser.add_argument(
        "--time-limit", "-t",
        type=int,
        default=540,
        metavar="SECONDS",
        help="OR-Tools zaman sınırı (saniye). Varsayılan: 540",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="DEBUG düzeyinde ayrıntılı log çıktısı.",
    )
    parser.add_argument(
        "--workers", "-w",
        type=int,
        default=1,
        metavar="N",
        help=(
            "Paralel tarih işleme için süreç sayısı. "
            "Varsayılan: 1 (sıralı). "
            "OR-Tools zaten num_search_workers=4 ile çok çekirdekli çalışır; "
            "workers=2 genellikle optimal denge sağlar."
        ),
    )
    return parser


# ─────────────────────────────────────────────────────────────────────────────
# Giriş Noktası
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    """
    Uygulama giriş noktası.

    Returns
    -------
    int
        0 → başarı, 1 → hata (atanamayan talep var ya da istisna).
    """
    parser = _build_arg_parser()
    args   = parser.parse_args()

    _configure_logging(verbose=args.verbose)

    # ── Veri Yükleme ─────────────────────────────────────────────────────────
    try:
        data = load_input(args.input)
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return 1
    except DataContractError as exc:
        log.error("Veri sözleşmesi ihlali: %s", exc)
        return 1

    # ── Tarih Seçimi ─────────────────────────────────────────────────────────
    dates = available_dates(data)

    if not dates:
        log.error("Veri setinde hiç planlama tarihi bulunamadı.")
        return 1

    if args.date:
        if args.date not in dates:
            log.error(
                "Belirtilen tarih '%s' veri setinde bulunamadı. "
                "Mevcut tarihler: %s",
                args.date, dates,
            )
            return 1
        selected_dates = [args.date]
    else:
        selected_dates = dates
        log.info(
            "Tarih belirtilmedi; %d tarih işlenecek: %s",
            len(dates), dates,
        )

    # ── Boru Hattı Çalıştırma ─────────────────────────────────────────────────
    all_results: list[PipelineResult] = []
    exit_code = 0
    n_workers = max(1, args.workers)

    if n_workers == 1 or len(selected_dates) == 1:
        # ── Sıralı mod ────────────────────────────────────────────────────────
        for date in selected_dates:
            result = run_pipeline(data, date, time_limit_sec=args.time_limit)
            print_result(result)
            all_results.append(result)
            if result.has_unassigned:
                log.warning(
                    "[%s] %d güzergâhta talep atanamadı.",
                    date, len(result.unassigned_demand),
                )
                exit_code = 1
    else:
        # ── Paralel mod: her tarih ayri surecte islenir ───────────────────────
        log.info(
            "%d tarih %d paralel surecle islenecek.",
            len(selected_dates), n_workers,
        )

        task_args = [(data, d, args.time_limit) for d in selected_dates]

        with concurrent.futures.ProcessPoolExecutor(max_workers=n_workers) as pool:
            futures = {
                pool.submit(_run_pipeline_for_date, arg): arg[1]
                for arg in task_args
            }
            for fut in concurrent.futures.as_completed(futures):
                date = futures[fut]
                try:
                    result = fut.result()
                except Exception as exc:
                    log.error("[%s] Islem hatasi: %s", date, exc)
                    exit_code = 1
                    continue
                print_result(result)
                all_results.append(result)
                if result.has_unassigned:
                    log.warning(
                        "[%s] %d guzergahta talep atanamadir.",
                        date, len(result.unassigned_demand),
                    )
                    exit_code = 1

        # Ciktiyi tarih sirasina gore yeniden sirala
        date_order = {d: i for i, d in enumerate(selected_dates)}
        all_results.sort(key=lambda r: date_order.get(r.date, 9999))

    # ── Çok Günlü Özet ───────────────────────────────────────────────────────
    if len(all_results) > 1:
        grand_total = sum(r.total_cost for r in all_results)
        print(f"{'═' * 72}")
        print(f"  GENEL TOPLAM ({len(all_results)} gün): {grand_total:>14,.1f} TL")
        print(f"{'═' * 72}\n")

    # ── JSON Çıktı ───────────────────────────────────────────────────────────
    if args.output:
        write_json_output(all_results, args.output)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
