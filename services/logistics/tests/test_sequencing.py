from uuid import uuid4

from logistics_service.clustering import cluster_orders
from logistics_service.sequencing import PartnerCandidate, path_km, pick_partner, sequence_points

HUB = (21.19, 81.28)


def test_sequence_visits_every_point_once_and_beats_the_given_order():
    # points along a line east of the hub, given in a scrambled order
    points = [(21.19, 81.28 + d) for d in (0.05, 0.01, 0.04, 0.02, 0.03)]
    order = sequence_points(HUB, points)
    assert sorted(order) == list(range(len(points)))
    assert order == [1, 3, 4, 2, 0]  # straight out along the line
    assert path_km(HUB, points, order) < path_km(HUB, points, range(len(points)))


def test_two_opt_removes_a_crossing():
    # nearest neighbour alone zig-zags here; 2-opt must not be longer than it
    points = [(21.20, 81.29), (21.18, 81.30), (21.21, 81.31), (21.17, 81.32)]
    order = sequence_points(HUB, points)
    nn_like = [0, 1, 2, 3]
    assert path_km(HUB, points, order) <= path_km(HUB, points, nn_like) + 1e-9


def test_small_inputs():
    assert sequence_points(HUB, []) == []
    assert sequence_points(HUB, [(21.2, 81.3)]) == [0]


def test_pick_partner_prefers_least_loaded_then_nearest():
    a = PartnerCandidate(uuid4(), "Asha", pools_today=1, home=(21.19, 81.28))
    b = PartnerCandidate(uuid4(), "Bharat", pools_today=0, home=(22.0, 82.0))
    c = PartnerCandidate(uuid4(), "Charu", pools_today=0, home=(21.20, 81.29))
    d = PartnerCandidate(uuid4(), "Deepak", pools_today=0, home=None)
    assert pick_partner([a, b, c, d], HUB) == c
    assert pick_partner([a, d], HUB) == d  # fewer pools wins over distance
    assert pick_partner([], HUB) is None


def test_cluster_caps_drops_per_pool():
    orders = [
        {
            "order_id": str(uuid4()),
            "hub_lat": 21.19,
            "hub_lng": 81.28,
            "drop_lat": 21.2 + i * 1e-4,
            "drop_lng": 81.3,
            "weight_kg": 1,
        }
        for i in range(20)
    ]
    pools = cluster_orders(orders, eps_km=3, capacity_kg=1000, max_orders=15)
    assert [len(p) for p in pools] == [15, 5]
