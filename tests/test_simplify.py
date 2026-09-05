from __future__ import annotations

import time

import numpy as np

from tests.conftest import segment
from traceart.core.simplify import douglas_peucker_mask, simplify_segment, simplify_track


def test_straight_line_reduced_to_endpoints(straight_track):
    out = simplify_track(straight_track, tolerance=0.001)
    assert out.n_points == 2


def test_corner_kept_or_dropped_per_tolerance():
    # Corde horizontale de (0,0) à (10,0) : le point médian s'en écarte
    # d'exactement 0.1.
    pts = np.asarray([(0.0, 0.0), (5.0, 0.1), (10.0, 0.0)])
    assert douglas_peucker_mask(pts, tolerance=1.0).tolist() == [True, False, True]
    assert douglas_peucker_mask(pts, tolerance=0.01).all()


def test_distance_is_to_the_chord_not_to_the_axis():
    # Corde oblique : l'écart du point médian vaut |5 - 0.1| / √2 ≈ 3.46,
    # pas 0.1. Un calcul fait sur un seul axe le raterait.
    pts = np.asarray([(0.0, 0.0), (5.0, 0.1), (10.0, 10.0)])
    assert douglas_peucker_mask(pts, tolerance=3.0).all()
    assert douglas_peucker_mask(pts, tolerance=4.0).tolist() == [True, False, True]


def test_endpoints_always_kept():
    pts = np.asarray([(float(i), 0.0) for i in range(100)])
    mask = douglas_peucker_mask(pts, tolerance=10.0)
    assert mask[0] and mask[-1]
    assert mask.sum() == 2


def test_closed_loop_does_not_collapse():
    # Extrémités confondues : la distance se mesure au point, pas à une
    # droite dégénérée, sinon la boucle entière disparaît.
    angles = np.linspace(0, 2 * np.pi, 64)
    pts = np.column_stack([np.cos(angles), np.sin(angles)])
    mask = douglas_peucker_mask(pts, tolerance=0.01)
    assert mask.sum() > 20


def test_elevation_stays_aligned():
    pts = [(float(i), 0.0) for i in range(11)]
    ele = [float(i * 10) for i in range(11)]
    seg = segment(pts, ele=ele)
    out = simplify_segment(seg, tolerance=0.001)
    assert len(out.ele) == len(out.coords)
    # Les altitudes conservées sont celles des points conservés.
    assert out.ele.tolist() == [0.0, 100.0]


def test_zero_tolerance_is_identity(straight_track):
    out = simplify_track(straight_track, tolerance=0.0)
    assert out.n_points == straight_track.n_points


def test_deep_recursion_would_overflow_but_stack_holds():
    # Dents de scie : chaque découpe isole le point voisin de la borne
    # gauche, donc la profondeur de découpe vaut N. Au-delà de 1000, une
    # implémentation récursive lèverait RecursionError.
    n = 5_000
    x = np.arange(n, dtype=np.float64)
    y = (x % 2) * 0.5
    mask = douglas_peucker_mask(np.column_stack([x, y]), tolerance=0.1)
    assert mask.all()


def test_realistic_large_track_is_fast():
    # 200 000 points de marche aléatoire : cas réaliste d'une trace GPS
    # dense. Les découpes restent équilibrées, le coût reste ~O(n log n).
    rng = np.random.default_rng(7)
    pts = np.cumsum(rng.normal(size=(200_000, 2)), axis=0)
    started = time.perf_counter()
    mask = douglas_peucker_mask(pts, tolerance=1.0)
    assert time.perf_counter() - started < 10.0
    assert 0 < mask.sum() < len(pts)


def test_deterministic():
    rng = np.random.default_rng(1234)
    pts = np.cumsum(rng.normal(size=(5000, 2)), axis=0)
    first = douglas_peucker_mask(pts, tolerance=0.5)
    second = douglas_peucker_mask(pts, tolerance=0.5)
    assert np.array_equal(first, second)
