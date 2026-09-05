from __future__ import annotations

from xml.etree import ElementTree

import pytest

from tests.conftest import segment
from traceart.core.gpx import parse_gpx
from traceart.core.model import Track
from traceart.pipeline import (
    Options,
    PipelineError,
    default_title,
    parse_aspect,
    run,
    run_each,
)

SVG_NS = "{http://www.w3.org/2000/svg}"


def test_extra_labels_are_drawn_regardless_of_zoom_tier(gpx_file):
    """Une ville ajoutée manuellement ignore toujours le filtre de
    rang/population des labels automatiques — c'est tout le sens de la
    fonctionnalité : l'utilisateur la demande précisément parce qu'elle
    n'apparaîtrait pas d'elle-même."""
    result = run(
        [gpx_file],
        Options(basemap="off"),
        extra_labels=[("MonHameau", 3.04, 44.03)],
    )
    assert "MonHameau" in result.svg
    assert result.dropped_labels == ()


def test_extra_labels_outside_the_frame_are_reported(gpx_file):
    """Aucun clip-path ne protège le SVG produit : un nom hors cadre ne
    doit pas se retrouver dans le document, juste signalé."""
    result = run(
        [gpx_file],
        Options(basemap="off"),
        extra_labels=[("TropLoin", 140.0, 44.03)],
    )
    assert "TropLoin" not in result.svg
    assert result.dropped_labels == ("TropLoin",)


def test_extra_labels_default_is_a_no_op(gpx_file):
    """`extra_labels=()` (défaut) ne doit rien changer à un rendu
    existant — zéro risque de régression pour tout appelant actuel."""
    with_default = run([gpx_file], Options(basemap="off"))
    explicit_empty = run([gpx_file], Options(basemap="off"), extra_labels=())
    assert with_default.svg == explicit_empty.svg
    assert with_default.dropped_labels == ()


def test_end_to_end_produces_valid_svg(gpx_file):
    result = run([gpx_file])
    assert ElementTree.fromstring(result.svg).tag == f"{SVG_NS}svg"
    assert result.points_rendered > 0
    assert result.stats[0].distance_m > 0


def test_default_options_need_no_configuration(gpx_file):
    # Priorité « zéro réglage obligatoire » : un GPX, une image.
    result = run([gpx_file])
    assert result.theme.name == "light"
    assert result.points_rendered > 0


def test_map_is_bare_by_default(gpx_file):
    """Ni titre, ni date, ni mesures : la trace et le fond, rien d'autre."""
    result = run([gpx_file])
    assert "Trace de test" not in result.svg
    assert "DISTANCE" not in result.svg
    assert "DÉNIVELÉ" not in result.svg
    # Pas de bandeau, donc pas de hauteur réservée en pied de page.
    assert result.layout.margins.bottom == result.layout.margins.top


def test_annotations_opt_in(gpx_file):
    result = run([gpx_file], Options(show_annotations=True))
    assert "Trace de test" in result.svg
    assert "DISTANCE" in result.svg
    assert result.layout.margins.bottom > result.layout.margins.top


def test_profile_alone_reserves_no_text_space(gpx_file):
    """Un profil sans bandeau texte ne doit pas laisser de bande vide."""
    from traceart.render.svg import Annotation, annotation_band_height

    bare = run([gpx_file], Options(show_profile=True))
    theme = bare.theme
    profile_only = annotation_band_height(theme, Annotation(show_profile=True))
    with_text = annotation_band_height(
        theme, Annotation(title="x", show_stats=True, show_profile=True)
    )
    assert 0 < profile_only < with_text
    assert profile_only < theme.num("page.annotation_band")


def test_simplification_reduces_points(gpx_file):
    dense = run([gpx_file], Options(tolerance=0.0))
    sparse = run([gpx_file], Options(tolerance=5.0))
    assert sparse.points_rendered < dense.points_rendered


def test_stats_measured_before_simplification(gpx_file):
    # Douglas-Peucker raccourcit la trace : la distance annoncée ne doit
    # pas dépendre de la tolérance.
    a = run([gpx_file], Options(tolerance=0.0))
    b = run([gpx_file], Options(tolerance=20.0))
    assert a.stats[0].distance_m == pytest.approx(b.stats[0].distance_m)


def test_output_is_deterministic(gpx_file):
    assert run([gpx_file]).svg == run([gpx_file]).svg


def test_all_themes_render(gpx_file):
    for name in ("light", "dark", "mono", "blueprint"):
        result = run([gpx_file], Options(theme=name))
        assert result.theme.name == name
        assert ElementTree.fromstring(result.svg) is not None


def test_trace_color_override_reaches_the_svg(gpx_file):
    result = run([gpx_file], Options(theme="dark", trace_color="#00ff00"))
    assert 'stroke="#00ff00"' in result.svg
    # Le reste du thème (fond) doit rester celui de "dark".
    assert result.theme.color("page.background") == "#14171a"


def test_no_trace_color_keeps_the_theme_palette(gpx_file):
    plain = run([gpx_file], Options(theme="dark"))
    overridden = run([gpx_file], Options(theme="dark", trace_color=None))
    assert plain.theme.trace_colors() == overridden.theme.trace_colors()


def test_multiple_files_share_one_frame(gpx_file, tmp_path):
    second = tmp_path / "second.gpx"
    second.write_text(gpx_file.read_text(encoding="utf-8"), encoding="utf-8")
    combined = run([gpx_file, second])
    assert len(combined.tracks) == 2
    assert len(combined.stats) == 2


def test_run_each_gives_one_result_per_file(gpx_file, tmp_path):
    second = tmp_path / "second.gpx"
    second.write_text(gpx_file.read_text(encoding="utf-8"), encoding="utf-8")
    results = run_each([gpx_file, second])
    assert len(results) == 2
    assert all(len(r.tracks) == 1 for _, r in results)


def test_no_input_rejected():
    with pytest.raises(PipelineError, match="aucun fichier"):
        run([])


def test_absurd_tolerance_reported(gpx_file):
    # Tout est réduit à deux points par segment, mais le SVG reste valide.
    result = run([gpx_file], Options(tolerance=10_000.0))
    assert result.points_rendered == 2 * len(result.tracks[0].segments) * len(result.tracks)


def test_annotations_can_be_disabled(gpx_file):
    result = run([gpx_file], Options(show_annotations=False))
    labels = {
        g.get("{http://www.inkscape.org/namespaces/inkscape}label")
        for g in ElementTree.fromstring(result.svg).iter(f"{SVG_NS}g")
    }
    assert not any((label or "").startswith("Annotations") for label in labels)


def test_profile_adds_a_layer(gpx_file):
    result = run([gpx_file], Options(show_profile=True))
    ids = {g.get("id") for g in ElementTree.fromstring(result.svg).iter(f"{SVG_NS}g")}
    assert "profile" in ids


def test_aspect_presets_and_ratios():
    assert parse_aspect("square") == 1.0
    assert parse_aspect("16:9") == pytest.approx(16 / 9)
    assert parse_aspect("0.7071") == pytest.approx(0.7071)
    assert parse_aspect("trace") is None
    assert parse_aspect(None) is None
    with pytest.raises(PipelineError, match="ratio invalide"):
        parse_aspect("portrait-ish")


def test_forced_aspect_changes_frame(gpx_file):
    square = run([gpx_file], Options(aspect=1.0))
    assert square.layout.width == pytest.approx(square.layout.height)


def test_default_title_skips_plumbing_directories(tmp_path):
    folder = tmp_path / "ALPES" / "GPX_INPUT"
    folder.mkdir(parents=True)
    tracks = [
        Track(segments=[segment([(3.0, 44.0), (3.1, 44.1)])], source=folder / f"P{i}.gpx")
        for i in (1, 2)
    ]
    assert default_title(tracks) == "Alpes"


def test_default_title_from_single_track_name():
    track = Track(segments=[segment([(3.0, 44.0), (3.1, 44.1)])], name="PARTIE_1_LES_ALPES")
    assert default_title([track]) == "Partie 1 Les Alpes"


def test_tracks_ordered_by_start_time(tmp_path):
    """Deux GPX nommés à l'envers de leur chronologie."""
    from tests.conftest import make_gpx, trkpt

    for name, day in (("z-premier.gpx", "01"), ("a-second.gpx", "02")):
        body = "<trk><trkseg>" + "".join(
            trkpt(3.0 + i * 0.01, 44.0, 100.0, f"2024-05-{day}T08:0{i}:00Z")
            for i in range(3)
        ) + "</trkseg></trk>"
        (tmp_path / name).write_text(make_gpx(body), encoding="utf-8")

    result = run(sorted(tmp_path.glob("*.gpx")))
    # `a-second.gpx` passe en premier par tri de fichier, mais sa date le
    # renvoie en second.
    assert result.stats[0].start_time.day == 1
    assert result.stats[1].start_time.day == 2


def test_order_preserved_without_timestamps(gpx_file, tmp_path):
    from tests.conftest import make_gpx, trkpt

    body = f"<trk><trkseg>{trkpt(9.0, 40.0)}{trkpt(9.1, 40.1)}</trkseg></trk>"
    second = tmp_path / "zzz.gpx"
    second.write_text(make_gpx(body), encoding="utf-8")
    result = run([second, gpx_file])
    # Tri alphabétique : `test.gpx` avant `zzz.gpx`.
    assert result.stats[0].start_time is not None


def test_date_like_track_name_falls_back_to_filename(tmp_path):
    from tests.conftest import make_gpx, trkpt

    body = (
        "<metadata><name>10/05/24</name></metadata>"
        f"<trk><trkseg>{trkpt(3.0, 44.0)}{trkpt(3.1, 44.1)}</trkseg></trk>"
    )
    path = tmp_path / "CEVENNES_JOUR_2.gpx"
    path.write_text(make_gpx(body), encoding="utf-8")
    track = parse_gpx(path)
    assert track.name == "10/05/24"
    assert default_title([track]) == "Cevennes Jour 2"
