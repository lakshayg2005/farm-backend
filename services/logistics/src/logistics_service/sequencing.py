"""Stop order inside a pool (HLD section 14, step 4) and partner choice (step 5).

Pure functions, so they are easy to test and need no database:

- `sequence_points`: nearest-neighbour from a start point, then 2-opt until no swap shortens the
  path. Used when OSRM has not already given an optimised order (offline or the public server
  is down). Haversine distances.
- `pick_partner`: the least-loaded partner for the day; ties go to the one nearest the pool's
  first pickup, then by name, so the choice is stable.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

Point = tuple[float, float]  # (lat, lng)


def haversine_km(a: Point, b: Point) -> float:
    lat1, lng1 = map(math.radians, a)
    lat2, lng2 = map(math.radians, b)
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    )
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def path_km(start: Point, points: Sequence[Point], order: Sequence[int]) -> float:
    """Length of start → points[order[0]] → points[order[1]] → ... (open path, no return)."""
    total, here = 0.0, start
    for i in order:
        total += haversine_km(here, points[i])
        here = points[i]
    return total


def _nearest_neighbour(start: Point, points: Sequence[Point]) -> list[int]:
    left = set(range(len(points)))
    order: list[int] = []
    here = start
    while left:
        nxt = min(left, key=lambda i: (haversine_km(here, points[i]), i))
        order.append(nxt)
        left.remove(nxt)
        here = points[nxt]
    return order


def _two_opt(start: Point, points: Sequence[Point], order: list[int]) -> list[int]:
    best = order[:]
    best_len = path_km(start, points, best)
    improved = True
    while improved:
        improved = False
        for i in range(len(best) - 1):
            for j in range(i + 1, len(best)):
                candidate = best[:i] + best[i : j + 1][::-1] + best[j + 1 :]
                length = path_km(start, points, candidate)
                if length < best_len - 1e-9:
                    best, best_len, improved = candidate, length, True
    return best


def sequence_points(start: Point, points: Sequence[Point]) -> list[int]:
    """Visiting order (indices into `points`) for a short open path starting at `start`."""
    if len(points) <= 1:
        return list(range(len(points)))
    return _two_opt(start, points, _nearest_neighbour(start, points))


@dataclass(frozen=True)
class PartnerCandidate:
    id: UUID
    name: str
    pools_today: int
    home: Point | None = None


def pick_partner(
    candidates: Sequence[PartnerCandidate], first_pickup: Point
) -> PartnerCandidate | None:
    if not candidates:
        return None

    def key(c: PartnerCandidate) -> tuple[int, float, str]:
        distance = haversine_km(c.home, first_pickup) if c.home else math.inf
        return (c.pools_today, distance, c.name)

    return min(candidates, key=key)
