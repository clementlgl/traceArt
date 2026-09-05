from __future__ import annotations

import math

import numpy as np
import pytest

from tests.conftest import segment
from traceart.core.model import Segment, Track
from traceart.core.stats import compute_stats


def test_distance_is_geodesic():
    # 1° de latitude ≈ 111,1 km.
    track = Track(segments=[segment([(3.0, 44.0), (3.0, 45.0)])])
    assert compute_stats(track).distance_km == pytest.approx(111.1, abs=0.5)


def test_elevation_noise_does_not_inflate_ascent():
    # Bruit de ±3 m autour d'un plateau : sous la bande morte de 5 m,
    # le D+ doit rester nul.
    n = 200
    noise = [500.0 + (3.0 if i % 2 else -3.0) for i in range(n)]
    seg = segment([(3.0 + i * 0.001, 44.0) for i in range(n)], ele=noise)
    stats = compute_stats(Track(segments=[seg]))
    assert stats.ascent_m == 0.0


def test_real_climb_is_counted():
    # 200 points, +10 m chacun. Le lissage rabote quelques mètres aux deux
    # extrémités de la série, d'où la tolérance relative.
    n = 200
    ele = [float(100 + i * 10) for i in range(n)]
    seg = segment([(3.0 + i * 0.001, 44.0) for i in range(n)], ele=ele)
    stats = compute_stats(Track(segments=[seg]))
    assert stats.ascent_m == pytest.approx(1990.0, rel=0.03)
    assert stats.descent_m == 0.0
    assert stats.ele_min == 100.0
    assert stats.ele_max == float(100 + (n - 1) * 10)


def test_descent_is_counted_separately():
    n = 100
    ele = [float(1000 - i * 5) for i in range(n)]
    seg = segment([(3.0 + i * 0.001, 44.0) for i in range(n)], ele=ele)
    stats = compute_stats(Track(segments=[seg]))
    assert stats.ascent_m == 0.0
    assert stats.descent_m == pytest.approx(495.0, rel=0.05)


def test_profile_is_continuous_across_segments():
    a = segment([(3.0, 44.0), (3.1, 44.0)], ele=[100.0, 120.0])
    b = segment([(3.2, 44.0), (3.3, 44.0)], ele=[130.0, 110.0])
    stats = compute_stats(Track(segments=[a, b]))
    # La distance cumulée ne repart pas de zéro au second segment. Le
    # saut géographique entre segments ne compte pas de distance : les
    # index 1 et 2 sont donc confondus, et la suite reste croissante.
    assert list(stats.profile_dist) == sorted(stats.profile_dist)
    assert stats.profile_dist[2] == pytest.approx(stats.profile_dist[1])
    assert stats.profile_dist[3] > stats.profile_dist[2]


def test_missing_elevation_is_reported_absent():
    track = Track(segments=[segment([(3.0, 44.0), (3.1, 44.1)])])
    stats = compute_stats(track)
    assert not stats.has_elevation
    assert math.isnan(stats.ele_min)


def test_duration_and_moving_time():
    coords = np.asarray([(3.0, 44.0), (3.01, 44.0), (3.01, 44.0), (3.02, 44.0)])
    seg = Segment(
        coords=coords,
        ele=np.full(4, math.nan),
        # 100 s de trajet, 500 s d'arrêt, 100 s de trajet.
        time=np.asarray([0.0, 100.0, 600.0, 700.0]),
    )
    stats = compute_stats(Track(segments=[seg]))
    assert stats.duration_s == pytest.approx(700.0)
    assert stats.moving_s == pytest.approx(200.0)
    assert stats.avg_speed_kmh > 0


def test_format_duration():
    track = Track(segments=[segment([(3.0, 44.0), (3.1, 44.1)])])
    stats = compute_stats(track)
    assert stats.format_duration(3600 * 12 + 60 * 34) == "12 h 34"
    assert stats.format_duration(60 * 34) == "34 min"
    assert stats.format_duration(0) == ""


def test_empty_track_rejected():
    with pytest.raises(ValueError, match="trace vide"):
        compute_stats(Track(segments=[]))
