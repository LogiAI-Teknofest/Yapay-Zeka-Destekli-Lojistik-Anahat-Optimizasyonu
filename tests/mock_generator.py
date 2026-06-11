import random
from typing import Dict, List

TM_IDS = ["İstanbul", "Yalova", "Kocaeli", "Tekirdağ"]
TM_NAMES = {
    "İstanbul": "Istanbul",
    "Yalova":   "Yalova",
    "Kocaeli":  "Kocaeli",
    "Tekirdağ": "Tekirdag",
}


def generate_test_packages(count: int = 18, seed: int = None) -> List[Dict[str, int]]:
    """Deterministik test paketi uretici (seed destekli)."""
    if seed is not None:
        random.seed(seed)

    count = max(15, min(20, count))
    packages = []

    for idx in range(1, count + 1):
        tm_id = random.choice(TM_IDS)
        desi = random.randint(10, 120)
        packages.append({
            "pkg_id": f"PKG_{idx:03d}",
            "tm_id": tm_id,
            "desi": desi,
        })

    return packages


if __name__ == "__main__":
    import json
    print(json.dumps(generate_test_packages(seed=42), indent=2, ensure_ascii=False))
