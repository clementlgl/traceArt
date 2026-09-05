from __future__ import annotations

import numpy as np
import pytest

from tests.conftest import segment
from traceart.core.model import Track
from traceart.core.project import choose_projection, project_track, unwrap_longitudes


def _track(points):
    return Track(segments=[segment(points)])


def test_auto_picks_lambert_for_local_track():
    proj = choose_projection([_track([(3.0, 44.0), (3.5, 44.5)])])
    assert proj.mode == "lcc"
    assert "+lon_0=3.25" in proj.definition


def test_auto_falls_back_to_mercator_for_world_span():
    proj = choose_projection([_track([(-150.0, -40.0), (150.0, 60.0)])])
    assert proj.mode == "webmercator"


def test_utm_zone_from_centroid():
    proj = choose_projection([_track([(3.0, 44.0), (3.1, 44.1)])], mode="utm")
    assert proj.definition == "EPSG:32631"
    south = choose_projection([_track([(3.0, -44.0), (3.1, -44.1)])], mode="utm")
    assert south.definition == "EPSG:32731"


def test_explicit_epsg_accepted():
    proj = choose_projection([_track([(3.0, 44.0), (3.1, 44.1)])], mode="EPSG:2154")
    assert proj.mode == "explicit"


def test_unknown_mode_rejected():
    with pytest.raises(ValueError, match="mode de projection inconnu"):
        choose_projection([_track([(3.0, 44.0), (3.1, 44.1)])], mode="mercateur")


def test_antimeridian_longitudes_unwrapped():
    lons = np.asarray([179.5, -179.5, 179.8])
    assert float(lons.max() - lons.min()) == pytest.approx(359.3)
    out = unwrap_longitudes(lons)
    # -179.5 devient 180.5 : l'étendue retombe à 1° au lieu de 359°.
    assert float(out.max() - out.min()) == pytest.approx(1.0)


def test_normal_longitudes_left_untouched():
    lons = np.asarray([-5.0, 3.0, 12.0])
    assert np.array_equal(unwrap_longitudes(lons), lons)


def test_projection_preserves_point_count():
    track = _track([(3.0, 44.0), (3.1, 44.1), (3.2, 44.05)])
    proj = choose_projection([track])
    out = project_track(track, proj)
    assert out.n_points == track.n_points
    assert out.crs == proj.definition


def test_projected_distances_are_metric():
    # 1° de latitude ≈ 111 km, quelle que soit la projection locale.
    track = _track([(3.0, 44.0), (3.0, 45.0)])
    out = project_track(track, choose_projection([track]))
    dy = abs(out.segments[0].coords[1, 1] - out.segments[0].coords[0, 1])
    assert 110_000 < dy < 112_000


def test_double_projection_rejected():
    track = _track([(3.0, 44.0), (3.1, 44.1)])
    proj = choose_projection([track])
    once = project_track(track, proj)
    with pytest.raises(ValueError, match="WGS84"):
        project_track(once, proj)
