from __future__ import annotations

import numpy as np
import pytest

from tests.conftest import segment
from traceart.core.model import Track
from traceart.render.layout import combined_bounds, fit_layout, layout_track

BOUNDS = (0.0, 0.0, 2000.0, 1000.0)


def test_content_ratio_matches_data_not_frame():
    # Le bandeau d'annotations ne doit pas comprimer la carte : c'est la
    # zone de contenu qui adopte le ratio des données.
    layout = fit_layout(BOUNDS, long_edge=1000.0, margin=50.0, annotation_band=150.0)
    assert layout.content_width / layout.content_height == pytest.approx(2.0)
    assert layout.width == pytest.approx(1000.0)


def test_no_wasted_space_around_content():
    layout = fit_layout(BOUNDS, long_edge=1000.0, margin=50.0, annotation_band=150.0)
    drawn_w = 2000.0 * layout.scale
    drawn_h = 1000.0 * layout.scale
    assert drawn_w == pytest.approx(layout.content_width)
    assert drawn_h == pytest.approx(layout.content_height)


def test_long_edge_is_respected_for_tall_data():
    layout = fit_layout((0.0, 0.0, 100.0, 2000.0), long_edge=1000.0, margin=40.0)
    assert max(layout.width, layout.height) == pytest.approx(1000.0)


def test_ribbon_frame_is_allowed():
    # Le cadre épouse la trace : un rapport 4:1 doit passer tel quel,
    # sans vide ajouté sur le petit côté.
    layout = fit_layout((0.0, 0.0, 4000.0, 1000.0), long_edge=1000.0, margin=40.0)
    assert layout.content_width / layout.content_height == pytest.approx(4.0)


def test_degenerate_ratio_is_clamped():
    # 1000:1 n'est plus une carte. Le garde-fou borne le rapport.
    layout = fit_layout((0.0, 0.0, 10_000.0, 10.0), long_edge=1000.0, margin=40.0)
    assert layout.content_width / layout.content_height <= 5.1


def test_single_scale_on_both_axes():
    layout = fit_layout(BOUNDS, long_edge=1000.0, margin=50.0, aspect=1.0)
    track = Track(segments=[segment([(0.0, 0.0), (2000.0, 1000.0)])])
    placed = layout_track(track, layout)
    coords = placed.segments[0].coords
    # Un carré de données doit rester carré : ratio des deltas conservé.
    dx = abs(coords[1, 0] - coords[0, 0])
    dy = abs(coords[1, 1] - coords[0, 1])
    assert dx / dy == pytest.approx(2.0)


def test_y_axis_is_flipped():
    layout = fit_layout(BOUNDS, long_edge=1000.0, margin=50.0)
    track = Track(segments=[segment([(0.0, 0.0), (2000.0, 1000.0)])])
    coords = layout_track(track, layout).segments[0].coords
    # Le point le plus au nord doit avoir le plus petit y en SVG.
    assert coords[1, 1] < coords[0, 1]


def test_content_stays_inside_margins():
    layout = fit_layout(BOUNDS, long_edge=1000.0, margin=50.0, annotation_band=120.0)
    track = Track(segments=[segment([(0.0, 0.0), (2000.0, 1000.0)])])
    coords = layout_track(track, layout).segments[0].coords
    assert coords[:, 0].min() >= layout.margins.left - 1e-6
    assert coords[:, 0].max() <= layout.width - layout.margins.right + 1e-6
    assert coords[:, 1].min() >= layout.margins.top - 1e-6
    assert coords[:, 1].max() <= layout.height - layout.margins.bottom + 1e-6


def test_degenerate_bounds_do_not_divide_by_zero():
    layout = fit_layout((5.0, 5.0, 5.0, 5.0), long_edge=1000.0, margin=50.0)
    assert np.isfinite(layout.scale)


def test_oversized_margins_rejected():
    with pytest.raises(ValueError, match="trop grand"):
        fit_layout(BOUNDS, long_edge=1000.0, margin=400.0, annotation_band=400.0)


def test_combined_bounds_spans_all_tracks():
    a = Track(segments=[segment([(0.0, 0.0), (1.0, 1.0)])])
    b = Track(segments=[segment([(-2.0, 5.0), (3.0, 7.0)])])
    assert combined_bounds([a, b]) == (-2.0, 0.0, 3.0, 7.0)


def test_invert_is_the_exact_inverse_of_apply():
    layout = fit_layout(BOUNDS, long_edge=1000.0, margin=50.0, annotation_band=120.0)
    source = np.asarray([[0.0, 0.0], [1234.5, 678.9], [2000.0, 1000.0]])
    assert layout.invert(layout.apply(source)) == pytest.approx(source)


def test_invert_reveals_area_outside_data_bounds():
    # Cadre carré imposé sur des données 2:1 : le cadre découvre du
    # terrain au nord et au sud de la bounding box de la trace.
    layout = fit_layout(BOUNDS, long_edge=1000.0, margin=0.0, aspect=1.0)
    corners = np.asarray([[0.0, 0.0], [layout.width, layout.height]])
    projected = layout.invert(corners)
    assert projected[:, 1].max() > 1000.0
    assert projected[:, 1].min() < 0.0
