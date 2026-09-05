from __future__ import annotations

import math

import numpy as np

from tests.conftest import segment
from traceart.core.clean import CleanConfig, clean_track
from traceart.core.model import Segment, Track


def _track(seg: Segment) -> Track:
    return Track(segments=[seg])


def test_duplicates_removed():
    pts = [(2.0, 45.0), (2.0, 45.0), (2.001, 45.0), (2.001, 45.0)]
    cleaned, report = clean_track(_track(segment(pts)), CleanConfig(enable_pauses=False))
    assert report.duplicates == 2
    assert cleaned.n_points == 2


def test_isolated_spike_removed():
    # 5 points alignés, le 3e projeté à 500 km : aller ET retour aberrants.
    pts = [(2.0, 45.0), (2.01, 45.0), (8.0, 45.0), (2.02, 45.0), (2.03, 45.0)]
    cleaned, report = clean_track(_track(segment(pts)), CleanConfig(enable_pauses=False))
    assert report.outliers == 1
    assert (cleaned.segments[0].coords[:, 0] < 3.0).all()


def test_legitimate_gap_is_kept():
    # Reprise d'enregistrement : un seul grand saut, suivi d'une suite
    # cohérente. Ce n'est pas un pic GPS, on ne doit rien retirer.
    pts = [(2.0, 45.0), (2.01, 45.0), (8.0, 45.0), (8.01, 45.0), (8.02, 45.0)]
    _, report = clean_track(_track(segment(pts)), CleanConfig(enable_pauses=False))
    assert report.outliers == 0


def test_impossible_speed_flagged_with_timestamps():
    coords = np.asarray([(2.0, 45.0), (2.01, 45.0), (5.0, 45.0), (2.02, 45.0), (2.03, 45.0)])
    n = len(coords)
    seg = Segment(
        coords=coords,
        ele=np.full(n, math.nan),
        # 10 s entre chaque point : le détour à 5° est physiquement exclu.
        time=np.arange(n, dtype=np.float64) * 10.0,
    )
    _, report = clean_track(_track(seg), CleanConfig(enable_pauses=False))
    assert report.outliers == 1


def test_pause_cluster_collapsed():
    # 20 points dans un rayon de ~1 m, puis un vrai déplacement.
    jitter = [(2.0 + i * 1e-6, 45.0 + i * 1e-6) for i in range(20)]
    moving = [(2.0 + i * 0.001, 45.0) for i in range(1, 6)]
    cleaned, report = clean_track(
        _track(segment(jitter + moving)),
        CleanConfig(enable_dedupe=False, enable_outliers=False, pause_radius_m=12.0),
    )
    assert report.paused >= 18
    assert cleaned.n_points == len(moving) + 1


def test_sharp_turns_survive_pause_filter():
    # Zigzag de 200 m d'amplitude : chaque point s'éloigne de l'ancre bien
    # au-delà du rayon d'immobilité, rien ne doit disparaître.
    pts = [(2.0 + i * 0.002, 45.0 + (i % 2) * 0.002) for i in range(10)]
    cleaned, _ = clean_track(_track(segment(pts)), CleanConfig())
    assert cleaned.n_points == len(pts)


def test_endpoints_always_kept():
    jitter = [(2.0 + i * 1e-7, 45.0) for i in range(50)]
    cleaned, _ = clean_track(_track(segment(jitter)), CleanConfig(enable_dedupe=False))
    seg = cleaned.segments[0]
    assert seg.coords[0, 0] == jitter[0][0]
    assert seg.coords[-1, 0] == jitter[-1][0]


def test_report_accounting_is_consistent():
    pts = [(2.0, 45.0), (2.0, 45.0), (2.01, 45.0), (2.02, 45.0)]
    cleaned, report = clean_track(_track(segment(pts)))
    assert report.points_in == 4
    assert report.points_out == cleaned.n_points
    assert report.removed == report.points_in - report.points_out
