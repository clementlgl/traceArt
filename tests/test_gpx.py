from __future__ import annotations

import numpy as np
import pytest

from tests.conftest import make_gpx, trkpt
from traceart.core.gpx import GpxError, parse_gpx, parse_many


def test_parse_segments_and_route(gpx_file):
    track = parse_gpx(gpx_file)
    # 2 trkseg + 1 rte
    assert len(track.segments) == 3
    assert track.n_points == 8
    assert track.name == "Trace de test"
    assert track.meta["creator"] == "test"


def test_waypoints_are_ignored(gpx_file):
    track = parse_gpx(gpx_file)
    all_coords = np.concatenate([s.coords for s in track.segments])
    # Le <wpt> parasite est à (3.0, 44.0) tout comme le 1er trkpt : on
    # vérifie plutôt qu'il n'a pas ajouté de point supplémentaire.
    assert len(all_coords) == 8


def test_elevation_and_time_parsed(gpx_file):
    seg = parse_gpx(gpx_file).segments[0]
    assert seg.ele[0] == 100.0
    assert seg.has_time
    assert seg.time[1] - seg.time[0] == pytest.approx(600.0)


def test_route_without_time_has_nan(gpx_file):
    route = parse_gpx(gpx_file).segments[2]
    assert not route.has_time
    assert route.has_ele


def test_works_without_namespace(tmp_path):
    body = f"<trk><trkseg>{trkpt(1.0, 43.0)}{trkpt(1.1, 43.1)}</trkseg></trk>"
    path = tmp_path / "nons.gpx"
    path.write_text(make_gpx(body, namespaced=False), encoding="utf-8")
    assert parse_gpx(path).n_points == 2


def test_invalid_coordinates_dropped(tmp_path):
    body = (
        "<trk><trkseg>"
        + trkpt(1.0, 43.0)
        + '<trkpt lat="200" lon="1.05"/>'
        + '<trkpt lat="nope" lon="1.06"/>'
        + trkpt(1.1, 43.1)
        + "</trkseg></trk>"
    )
    path = tmp_path / "bad.gpx"
    path.write_text(make_gpx(body), encoding="utf-8")
    assert parse_gpx(path).n_points == 2


def test_single_point_segment_dropped(tmp_path):
    body = f"<trk><trkseg>{trkpt(1.0, 43.0)}</trkseg></trk>"
    path = tmp_path / "one.gpx"
    path.write_text(make_gpx(body), encoding="utf-8")
    with pytest.raises(GpxError, match="aucun point de trace"):
        parse_gpx(path)


def test_missing_file_raises(tmp_path):
    with pytest.raises(GpxError):
        parse_gpx(tmp_path / "absent.gpx")


def test_parse_many_is_sorted_and_skips_failures(tmp_path, gpx_file):
    broken = tmp_path / "aaa-broken.gpx"
    broken.write_text("<gpx><trk>", encoding="utf-8")
    tracks = parse_many([gpx_file, broken])
    assert len(tracks) == 1
    with pytest.raises(GpxError):
        parse_many([broken], strict=True)
