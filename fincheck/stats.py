"""Uncertainty for pass rates: Wilson intervals and paired bootstrap differences."""

import math
import random


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a binomial proportion."""
    if total == 0:
        return (0.0, 1.0)
    p = successes / total
    centre = (p + z * z / (2 * total)) / (1 + z * z / total)
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return (max(0.0, centre - half), min(1.0, centre + half))


def paired_bootstrap(a: list[int], b: list[int], resamples: int = 1000, seed: int = 0) -> dict:
    """Difference in mean outcome (a - b) on the same cases, with a 95% percentile interval."""
    if len(a) != len(b) or not a:
        raise ValueError("Paired outcomes must be non-empty and the same length")
    rng = random.Random(seed)
    n = len(a)
    diffs = []
    for _ in range(resamples):
        picks = [rng.randrange(n) for _ in range(n)]
        diffs.append(sum(a[i] - b[i] for i in picks) / n)
    diffs.sort()
    low, high = diffs[int(0.025 * resamples)], diffs[min(resamples - 1, int(0.975 * resamples))]
    return {"difference": sum(x - y for x, y in zip(a, b)) / n, "ci95": [low, high],
            "resamples": resamples, "seed": seed,
            "inconclusive": low <= 0 <= high,
            "both": sum(1 for x, y in zip(a, b) if x and y), "only_a": sum(1 for x, y in zip(a, b) if x and not y),
            "only_b": sum(1 for x, y in zip(a, b) if y and not x), "neither": sum(1 for x, y in zip(a, b) if not x and not y)}


def p95(values: list[float]) -> float | None:
    """Sample 95th percentile (nearest rank)."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]
