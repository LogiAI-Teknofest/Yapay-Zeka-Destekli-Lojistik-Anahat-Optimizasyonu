import random
from typing import Dict, List

TM_IDS = ["İstanbul", "Yalova", "Kocaeli", "Tekirdağ"]


def generate_tm_demand_items(count: int = 18, seed: int = None) -> List[Dict]:
    """Deterministik test talebi uretici (seed destekli). Desi bazlıdır, paket değil."""
    if seed is not None:
        random.seed(seed)

    count = max(15, min(20, count))
    items = []

    for idx in range(1, count + 1):
        tm_id = random.choice(TM_IDS)
        desi = random.randint(10, 120)
        items.append({
            "pkg_id": f"ITEM_{idx:03d}",
            "tm_id": tm_id,
            "desi": desi,
        })

    return items


if __name__ == "__main__":
    import json
    print(json.dumps(generate_tm_demand_items(seed=42), indent=2, ensure_ascii=False))
