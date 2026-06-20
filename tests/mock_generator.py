import random
from typing import Dict, List, Optional

# Yalnızca standalone (python mock_generator.py) çalıştırma için son-çare fallback.
# Operasyonel/test yolunda şehirler JSON sözleşmesinden türetilip `tm_ids` ile geçilir
# (Kural 4 — SSoT: hardcode parametre kullanılmaz).
_FALLBACK_TM_IDS = ["İstanbul", "Yalova", "Kocaeli", "Tekirdağ"]


def generate_tm_demand_items(
    count: int = 18,
    seed: int = None,
    tm_ids: Optional[List[str]] = None,
) -> List[Dict]:
    """
    Deterministik test talebi üretici (seed destekli). Desi bazlıdır, paket değil.

    tm_ids : TM/şehir isimleri. Verilmezse fallback kullanılır; çağıranın
             SSoT gereği JSON'dan türetilmiş listeyi geçmesi beklenir.
    """
    # İzole RNG: global random durumunu bozmaz (seed=None ise sistem entropisi).
    rng = random.Random(seed)
    ids = list(tm_ids) if tm_ids else _FALLBACK_TM_IDS

    count = max(1, count)
    items = []
    for idx in range(1, count + 1):
        items.append({
            "pkg_id": f"ITEM_{idx:03d}",
            "tm_id": rng.choice(ids),
            "desi": rng.randint(10, 120),
        })
    return items


if __name__ == "__main__":
    import json
    print(json.dumps(generate_tm_demand_items(seed=42), indent=2, ensure_ascii=False))
