"""Unit tests for the DBSCAN clustering module (no DB required)."""

from __future__ import annotations

from uuid import uuid4

from logistics_service.clustering import cluster_orders, haversine_km


def _order(
    drop_lat: float,
    drop_lng: float,
    weight_kg: float = 5.0,
    hub_lat: float = 21.1,
    hub_lng: float = 81.3,
) -> dict:
    return {
        "order_id": uuid4(),
        "hub_lat": hub_lat,
        "hub_lng": hub_lng,
        "drop_lat": drop_lat,
        "drop_lng": drop_lng,
        "weight_kg": weight_kg,
    }


class TestHaversine:
    def test_same_point_is_zero(self) -> None:
        assert haversine_km(21.1, 81.3, 21.1, 81.3) == 0.0

    def test_known_distance(self) -> None:
        # Raipur (21.25, 81.63) to Durg (21.19, 81.28) is ~36 km straight-line
        km = haversine_km(21.2514, 81.6296, 21.1927, 81.2831)
        assert 30 < km < 42


class TestClusterOrders:
    def test_empty_returns_empty(self) -> None:
        assert cluster_orders([]) == []

    def test_single_order_one_pool(self) -> None:
        pools = cluster_orders([_order(21.1, 81.3)])
        assert len(pools) == 1
        assert len(pools[0]) == 1

    def test_nearby_orders_same_cluster(self) -> None:
        # Two points ~0.5 km apart → same cluster
        pools = cluster_orders(
            [
                _order(21.100, 81.300),
                _order(21.104, 81.304),  # ~0.6 km
            ],
            eps_km=3.0,
        )
        assert len(pools) == 1
        assert len(pools[0]) == 2

    def test_distant_orders_separate_clusters(self) -> None:
        # Two points ~40 km apart → different clusters
        pools = cluster_orders(
            [
                _order(21.1, 81.3),
                _order(21.5, 81.6),
            ],
            eps_km=3.0,
        )
        assert len(pools) == 2

    def test_capacity_splitting(self) -> None:
        # Three 25 kg orders near each other → split into 2 pools (capacity=50 kg)
        orders = [_order(21.100 + i * 0.001, 81.300, weight_kg=25.0) for i in range(3)]
        pools = cluster_orders(orders, eps_km=3.0, capacity_kg=50.0)
        assert len(pools) == 2
        # First pool has 2 orders (50 kg), second has 1 (25 kg)
        assert len(pools[0]) == 2
        assert len(pools[1]) == 1

    def test_no_capacity_limit(self) -> None:
        orders = [_order(21.100 + i * 0.001, 81.300, weight_kg=100.0) for i in range(5)]
        # capacity_kg=0 means no splitting
        pools = cluster_orders(orders, eps_km=3.0, capacity_kg=0.0)
        assert len(pools) == 1

    def test_mixed_near_and_far(self) -> None:
        near = [_order(21.100 + i * 0.001, 81.300) for i in range(3)]
        far = [_order(21.500 + i * 0.001, 81.600) for i in range(2)]
        pools = cluster_orders(near + far, eps_km=3.0)
        sizes = sorted(len(p) for p in pools)
        assert sizes == [2, 3]


class TestSharedDeliveryBatch:
    """The batch sees many orders at once: clusters must stay compact and near their farms."""

    def test_a_long_strip_is_cut_into_compact_pools_not_in_arrival_order(self) -> None:
        # a strip running north, 1 km apart; the orders arrive shuffled
        strip = [_order(21.0 + i * 0.009, 81.3, weight_kg=10.0) for i in range(8)]
        shuffled = [strip[i] for i in (0, 7, 1, 6, 2, 5, 3, 4)]
        pools = cluster_orders(shuffled, eps_km=2.0, capacity_kg=0.0, max_orders=4)
        assert sorted(len(p) for p in pools) == [4, 4]
        for pool in pools:
            lats = sorted(o["drop_lat"] for o in pool)
            assert lats[-1] - lats[0] < 0.04  # 4 consecutive points, not both ends

    def test_capacity_still_splits_a_strip_along_its_length(self) -> None:
        strip = [_order(21.0 + i * 0.009, 81.3, weight_kg=20.0) for i in range(6)]
        pools = cluster_orders(strip[::-1], eps_km=2.0, capacity_kg=60.0, max_orders=15)
        assert [len(p) for p in pools] == [3, 3]
        assert max(o["drop_lat"] for o in pools[0]) < min(o["drop_lat"] for o in pools[1])

    def test_customers_next_door_but_farms_far_apart_are_not_pooled(self) -> None:
        a = _order(21.100, 81.300)
        b = _order(21.101, 81.301)
        b["hub_lat"], b["hub_lng"] = 23.0, 83.0  # a farm ~280 km from a's farm
        assert len(cluster_orders([a, b], eps_km=5.0)) == 1  # unchanged without the farm limit
        assert len(cluster_orders([a, b], eps_km=5.0, hub_eps_km=30.0)) == 2
