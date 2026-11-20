"""DBSCAN-based clustering of delivery orders into pools.

Algorithm (HLD section 10 / issue #19):
1. Collect all orders ready for dispatch on a given date.
2. Cluster drop locations with DBSCAN (eps ≈ 3 km, haversine metric,
   min_samples=1 so every order lands in exactly one cluster).
3. Split a cluster into sub-clusters when total weight > partner capacity.
4. Each sub-cluster becomes one DeliveryPool.

The function returns a list of clusters; each cluster is a list of dicts:
  {"order_id": UUID, "hub_lat": float, "hub_lng": float,
   "drop_lat": float, "drop_lng": float, "weight_kg": float}
"""

from __future__ import annotations

import math

# ---------------------------------------------------------------------------
# Haversine distance (km) – used for capacity-aware splitting without sklearn
# ---------------------------------------------------------------------------

EARTH_KM = 6371.0


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Return the great-circle distance in kilometres."""
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    )
    return 2 * EARTH_KM * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# DBSCAN (pure-Python, no sklearn dependency required)
# ---------------------------------------------------------------------------

Order = dict  # {"order_id": UUID, "hub_lat", "hub_lng", "drop_lat", "drop_lng", "weight_kg"}


def _dbscan(
    orders: list[Order],
    eps_km: float,
) -> list[list[Order]]:
    """Minimal DBSCAN with haversine metric, min_samples=1 (every point is a core point).

    Returns a list of clusters; each cluster is a non-empty list of orders.
    """
    n = len(orders)
    visited = [False] * n
    cluster_id = [-1] * n
    clusters: list[list[int]] = []

    def neighbours(idx: int) -> list[int]:
        lat1, lng1 = orders[idx]["drop_lat"], orders[idx]["drop_lng"]
        return [
            j
            for j in range(n)
            if j != idx
            and haversine_km(lat1, lng1, orders[j]["drop_lat"], orders[j]["drop_lng"]) <= eps_km
        ]

    next_cluster = 0
    for i in range(n):
        if visited[i]:
            continue
        visited[i] = True
        nbrs = neighbours(i)
        # min_samples=1: every point seeds a cluster
        cid = next_cluster
        next_cluster += 1
        cluster_id[i] = cid
        clusters.append([i])

        queue = list(nbrs)
        while queue:
            j = queue.pop()
            if not visited[j]:
                visited[j] = True
                new_nbrs = neighbours(j)
                queue.extend(new_nbrs)
            if cluster_id[j] == -1:
                cluster_id[j] = cid
                clusters[cid].append(j)

    return [[orders[i] for i in cluster] for cluster in clusters]


# ---------------------------------------------------------------------------
# Capacity-aware splitting
# ---------------------------------------------------------------------------


def _split_by_capacity(cluster: list[Order], capacity_kg: float) -> list[list[Order]]:
    """Split a cluster into sub-clusters, each within capacity_kg."""
    if capacity_kg <= 0:
        return [cluster]
    sub_clusters: list[list[Order]] = []
    current: list[Order] = []
    current_weight = 0.0
    for order in cluster:
        w = order.get("weight_kg") or 0.0
        if current and current_weight + w > capacity_kg:
            sub_clusters.append(current)
            current = []
            current_weight = 0.0
        current.append(order)
        current_weight += w
    if current:
        sub_clusters.append(current)
    return sub_clusters


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def cluster_orders(
    orders: list[Order],
    eps_km: float = 3.0,
    capacity_kg: float = 50.0,
    max_orders: int = 15,
) -> list[list[Order]]:
    """Cluster `orders` into delivery pools.

    Returns a list of pools, each a list of order dicts satisfying:
    - All drop locations within ~eps_km of each other.
    - Total weight_kg ≤ capacity_kg (or best-effort if a single order exceeds it).
    - At most `max_orders` orders (one drop each).
    """
    if not orders:
        return []
    raw_clusters = _dbscan(orders, eps_km)
    pools: list[list[Order]] = []
    for cluster in raw_clusters:
        for sub in _split_by_capacity(cluster, capacity_kg):
            # also cap the number of drops per vehicle
            for start in range(0, len(sub), max(1, max_orders)):
                chunk = sub[start : start + max(1, max_orders)]
                if chunk:
                    pools.append(chunk)
    return pools
