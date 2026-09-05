"""Tests du fond de carte sur des FlatGeobuf synthétiques.

Aucun accès réseau : on fabrique des jeux qui portent les mêmes noms et
les mêmes champs que Natural Earth, ce qui exerce le filtrage par
importance, la découpe et la reprojection sans télécharger 50 Mo.
"""

from __future__ import annotations

import numpy as np
import pytest

from tests.conftest import TRACK, _tier
from traceart.basemap.catalog import CATALOG, dataset_by_name, datasets_for
from traceart.basemap.query import (
    BasemapError,
    build_basemap,
    suggest_cities,
    visible_bounds_wgs84,
)
from traceart.basemap.store import Entry, Store, StoreError, missing_datasets
from traceart.basemap.tiers import choose_tier
from traceart.core.project import choose_projection
from traceart.render.layout import fit_layout

# ---------------------------------------------------------------- catalogue


def test_url_pattern():
    dataset = dataset_by_name("ne_10m_coastline")
    assert dataset.url == (
        "https://naturalearth.s3.amazonaws.com/10m_physical/ne_10m_coastline.zip"
    )


def test_every_dataset_has_a_theme_layer():
    from traceart.basemap.catalog import AVAILABLE_LAYERS

    assert {d.layer for d in CATALOG} <= set(AVAILABLE_LAYERS)


def test_roads_fall_back_to_the_only_available_scale():
    # Natural Earth ne publie les routes qu'en 10m.
    assert [d.scale for d in datasets_for("roads", "110m")] == ["10m"]


def test_national_borders_exist_at_every_scale():
    for scale in ("10m", "50m", "110m"):
        assert {d.scale for d in datasets_for("borders", scale)} == {scale}


def test_internal_boundaries_absent_from_110m_fall_back():
    """Natural Earth ne publie `admin_1_states_provinces_lines` qu'en
    10m et 50m : à 110m on retombe sur la résolution disponible."""
    assert {d.scale for d in datasets_for("boundaries", "110m")} == {"10m"}


# -------------------------------------------------------------------- tiers


@pytest.mark.parametrize(
    ("span", "scale"),
    [(60.0, "110m"), (20.0, "50m"), (5.0, "10m"), (0.3, "10m")],
)
def test_tier_scales_with_span(span, scale):
    assert choose_tier((0.0, 0.0, span, span)).scale == scale


def test_local_tier_keeps_more_detail_than_world():
    assert choose_tier((0.0, 0.0, 0.2, 0.2)).max_rank > choose_tier((0.0, 0.0, 90.0, 60.0)).max_rank


# ------------------------------------------------------------------- store


def test_manifest_roundtrip_is_sorted(tmp_path):
    store = Store(tmp_path)
    store.write_manifest(
        {
            "b": Entry("b", "http://b", "y", 2),
            "a": Entry("a", "http://a", "x", 1),
        }
    )
    text = store.manifest_path.read_text(encoding="utf-8")
    assert text.index('"a"') < text.index('"b"')
    assert store.read_manifest()["a"].features == 1


def test_corrupt_manifest_reported(tmp_path):
    store = Store(tmp_path)
    store.root.mkdir(parents=True, exist_ok=True)
    store.manifest_path.write_text("{ pas du json", encoding="utf-8")
    with pytest.raises(StoreError, match="manifeste illisible"):
        store.read_manifest()


def test_missing_datasets_listed(tmp_path, store):
    empty = Store(tmp_path / "vide")
    assert missing_datasets(empty, list(datasets_for("water", "10m")))
    assert not missing_datasets(store, list(datasets_for("water", "10m")))


def test_status_covers_whole_catalog(store):
    rows = store.status()
    assert len(rows) == len(CATALOG)
    present = {d.name for d, ok, _ in rows if ok}
    assert "ne_10m_coastline" in present
    assert "ne_110m_coastline" not in present


def test_reading_absent_dataset_is_reported(tmp_path, frame):
    projector, layout = frame
    with pytest.raises(StoreError, match="absent du cache"):
        build_basemap(
            layout, projector, layers=("water",), store=Store(tmp_path / "vide")
        )


# ------------------------------------------------------------------ requête


def test_visible_bounds_contain_the_track(frame):
    projector, layout = frame
    min_lon, min_lat, max_lon, max_lat = visible_bounds_wgs84(layout, projector)
    assert min_lon < 3.2 and max_lon > 3.8
    assert min_lat < 44.0 and max_lat > 44.3


def test_forced_aspect_widens_visible_bounds():
    from traceart.core.project import project_track

    projector = choose_projection([TRACK])
    projected = project_track(TRACK, projector)
    tight = fit_layout(projected.bounds(), long_edge=1000.0, margin=56.0)
    square = fit_layout(projected.bounds(), long_edge=1000.0, margin=56.0, aspect=1.0)
    tight_box = visible_bounds_wgs84(tight, projector)
    square_box = visible_bounds_wgs84(square, projector)
    # Le cadre carré découvre du terrain au nord et au sud.
    assert (square_box[3] - square_box[1]) > (tight_box[3] - tight_box[1])


def test_layers_are_built_in_viewbox_units(store, frame):
    projector, layout = frame
    result = build_basemap(
        layout, projector, layers=("water", "coastline", "rivers"), store=store
    )
    assert result.tier.scale == "10m"
    assert set(result.layers) <= {"water", "coastline", "rivers"}
    for paths in result.layers.values():
        for coords, _closed in paths:
            assert coords[:, 0].min() >= -1.0
            assert coords[:, 0].max() <= layout.width + 1.0


def test_polygons_are_marked_closed(store, frame):
    projector, layout = frame
    result = build_basemap(layout, projector, layers=("water",), store=store)
    assert all(closed for _, closed in result.layers["water"])


def test_lines_are_not_marked_closed(store, frame):
    projector, layout = frame
    result = build_basemap(layout, projector, layers=("coastline",), store=store)
    assert all(not closed for _, closed in result.layers["coastline"])


def test_track_of_half_a_degree_gets_the_local_tier(store, frame):
    projector, layout = frame
    result = build_basemap(layout, projector, layers=("rivers",), store=store)
    assert result.tier.name == "local"


def test_minor_rivers_kept_locally_dropped_regionally(store, frame):
    projector, layout = frame
    local = _tier("local")
    regional = _tier("région")
    detailed = build_basemap(
        layout, projector, layers=("rivers",), store=store, tier=local
    )
    coarse = build_basemap(
        layout, projector, layers=("rivers",), store=store, tier=regional
    )
    # Rang 11 : sous le seuil local (12), au-dessus du seuil régional (8).
    assert len(detailed.layers["rivers"]) == 2
    assert len(coarse.layers["rivers"]) == 1


def test_tiny_lake_is_dropped(store, frame):
    projector, layout = frame
    result = build_basemap(layout, projector, layers=("water",), store=store)
    # L'océan et le grand lac restent ; le lac de 10 m de côté disparaît.
    assert len(result.layers["water"]) == 2


def test_labels_filtered_by_population(store, frame):
    projector, layout = frame
    result = build_basemap(layout, projector, layers=("labels",), store=store)
    # Palier local : 15 000 habitants minimum. Petitbourg (3 000) tombe.
    assert result.tier.min_population == 15_000
    assert [lbl.text for lbl in result.labels] == ["Grandville"]


def test_suggest_cities_bypasses_the_tier_population_floor(store, frame):
    """Contrairement au fond automatique (`test_labels_filtered_by_
    population`), les suggestions ignorent le seuil de population du
    palier — c'est le but : proposer Petitbourg, que le palier local
    (15 000 habitants minimum) écarterait sinon."""
    projector, layout = frame
    bbox = visible_bounds_wgs84(layout, projector)
    tier = choose_tier(bbox)
    names = [name for name, _lon, _lat in suggest_cities(bbox, tier, store=store)]
    assert names == ["Grandville", "Petitbourg"]


def test_suggest_cities_respects_the_limit(store, frame):
    projector, layout = frame
    bbox = visible_bounds_wgs84(layout, projector)
    tier = choose_tier(bbox)
    names = [
        name for name, _lon, _lat in suggest_cities(bbox, tier, store=store, limit=1)
    ]
    assert names == ["Grandville"]


def test_labels_sorted_by_population_descending(store, frame):
    projector, layout = frame
    # Palier sans seuil de population : les deux villes passent, la plus
    # peuplée en premier pour que toute troncature garde l'essentiel.
    from traceart.basemap.tiers import Tier

    permissive = Tier("test", 0.0, "10m", max_rank=20, min_population=0)
    result = build_basemap(
        layout, projector, layers=("labels",), store=store, tier=permissive
    )
    assert [lbl.text for lbl in result.labels] == ["Grandville", "Petitbourg"]


def test_labels_are_placed_inside_the_frame(store, frame):
    projector, layout = frame
    result = build_basemap(layout, projector, layers=("labels",), store=store)
    for label in result.labels:
        assert 0 <= label.x <= layout.width
        assert 0 <= label.y <= layout.height


def test_unknown_layer_rejected(store, frame):
    projector, layout = frame
    with pytest.raises(BasemapError, match="inconnue"):
        build_basemap(layout, projector, layers=("montagnes",), store=store)


def test_build_is_deterministic(store, frame):
    projector, layout = frame
    first = build_basemap(
        layout, projector, layers=("water", "rivers", "labels"), store=store
    )
    second = build_basemap(
        layout, projector, layers=("water", "rivers", "labels"), store=store
    )
    assert first.labels == second.labels
    for layer, paths in first.layers.items():
        for (a, ca), (b, cb) in zip(paths, second.layers[layer], strict=True):
            assert np.array_equal(a, b)
            assert ca == cb


# ------------------------------------------------------------------ pipeline


def test_pipeline_draws_the_basemap(store, gpx_file, tmp_path):
    """Le fixture couvre les Cévennes ; on y place un GPX de test."""
    from xml.etree import ElementTree

    from tests.conftest import make_gpx, trkpt
    from traceart.pipeline import Options, run

    body = "<trk><trkseg>" + "".join(
        trkpt(3.3 + i * 0.05, 44.05 + i * 0.03) for i in range(6)
    ) + "</trkseg></trk>"
    path = tmp_path / "cevennes.gpx"
    path.write_text(make_gpx(body), encoding="utf-8")

    result = run(
        [path],
        Options(basemap="on", cache_dir=store.root, layers=("water", "rivers")),
    )
    ids = {
        g.get("id")
        for g in ElementTree.fromstring(result.svg).iter("{http://www.w3.org/2000/svg}g")
    }
    assert "basemap-water" in ids
    assert result.tier is not None
    assert result.basemap_note is None


def test_pipeline_auto_mode_skips_missing_cache(gpx_file, tmp_path):
    from traceart.pipeline import Options, run

    result = run([gpx_file], Options(basemap="auto", cache_dir=tmp_path / "vide"))
    assert result.basemap_note is not None
    # Le message nomme la résolution manquante, pas juste « cache vide ».
    assert "10m" in result.basemap_note
    assert "data fetch" in result.basemap_note


def test_pipeline_on_mode_demands_the_cache(gpx_file, tmp_path):
    from traceart.errors import MissingDataError
    from traceart.pipeline import Options, run

    with pytest.raises(MissingDataError, match="ne_10m_ocean") as exc:
        run([gpx_file], Options(basemap="on", cache_dir=tmp_path / "vide"))
    assert "ne_10m_ocean" in exc.value.datasets
    assert exc.value.scale == "10m"


def test_pipeline_off_mode_needs_no_cache(gpx_file, tmp_path):
    from traceart.pipeline import Options, run

    result = run([gpx_file], Options(basemap="off", cache_dir=tmp_path / "vide"))
    assert result.basemap_note is None
    assert result.tier is None


def test_unknown_basemap_mode_rejected(gpx_file):
    from traceart.pipeline import Options, PipelineError, run

    with pytest.raises(PipelineError, match="mode de fond"):
        run([gpx_file], Options(basemap="peut-être"))


def test_basemap_never_reaches_the_annotation_band(store, tmp_path):
    """Un plan d'eau ne doit jamais se peindre par-dessus le titre."""
    from traceart.basemap.query import frame_rect
    from traceart.core.project import project_track

    projector = choose_projection([TRACK])
    projected = project_track(TRACK, projector)
    layout = fit_layout(
        projected.bounds(), long_edge=1000.0, margin=56.0, annotation_band=160.0
    )
    _, _, _, bottom = frame_rect(layout, bleed=True)
    assert bottom == layout.height - layout.margins.bottom

    result = build_basemap(
        layout, projector, layers=("water", "rivers"), store=store, bleed=True
    )
    for paths in result.layers.values():
        for coords, _ in paths:
            assert coords[:, 1].max() <= bottom + 1e-6


def test_bleed_reaches_the_frame_edges(store, frame):
    from traceart.basemap.query import frame_rect

    projector, layout = frame
    left, top, right, _ = frame_rect(layout, bleed=True)
    assert (left, top, right) == (0.0, 0.0, layout.width)
    inset_left, inset_top, inset_right, _ = frame_rect(layout, bleed=False)
    assert inset_left == layout.margins.left
    assert inset_top == layout.margins.top
    assert inset_right == layout.width - layout.margins.right


def test_no_bleed_keeps_basemap_inside_margins(store, frame):
    projector, layout = frame
    result = build_basemap(
        layout, projector, layers=("water",), store=store, bleed=False
    )
    for coords, _ in result.layers["water"]:
        assert coords[:, 0].min() >= layout.margins.left - 1e-6
        assert coords[:, 1].min() >= layout.margins.top - 1e-6


def test_internal_boundaries_only_at_local_tier(store, frame):
    projector, layout = frame
    local = build_basemap(
        layout, projector, layers=("boundaries",), store=store, tier=_tier("local")
    )
    regional = build_basemap(
        layout, projector, layers=("boundaries",), store=store, tier=_tier("région")
    )
    # 96 départements à l'échelle d'un pays saturent le fond.
    assert local.layers.get("boundaries")
    assert not regional.layers.get("boundaries")


def test_national_borders_shown_at_every_tier(store, frame):
    """Une frontière structure la carte à toutes les échelles."""
    projector, layout = frame
    for name in ("région", "local"):
        result = build_basemap(
            layout, projector, layers=("borders",), store=store, tier=_tier(name)
        )
        assert result.layers.get("borders")


def test_borders_and_boundaries_are_styled_apart():
    from traceart.render.theme import LAYER_ORDER, load_theme

    theme = load_theme("light")
    border = theme.layer("borders")
    internal = theme.layer("boundaries")
    # La frontière doit être plus affirmée que la limite interne.
    assert border["width"] > internal["width"]
    assert border["dash"] != internal["dash"]
    assert border["stroke"] != internal["stroke"]
    # Et dessinée par-dessus, là où les deux se superposent.
    assert LAYER_ORDER.index("borders") > LAYER_ORDER.index("boundaries")


def test_national_borders_stay_natural_earth_under_osm():
    """Une relation de pays est tronquée au bord d'un extrait : son
    contour dessinerait un artefact rectiligne le long de la coupe."""
    from traceart.basemap.query import OSM_REPLACES

    assert "boundaries" in OSM_REPLACES
    assert "borders" not in OSM_REPLACES


def test_optional_supplements_do_not_block_rendering(store, frame):
    """Un cache rempli avant l'ajout d'un supplément reste utilisable."""
    projector, layout = frame
    supplements = [d for d in datasets_for("water", "10m") if d.optional]
    assert supplements, "les lacs régionaux doivent être des suppléments"
    assert all(not store.has(d) for d in supplements)

    # Absents du cache, mais le rendu passe et garde l'océan et le lac.
    assert not missing_datasets(store, list(datasets_for("water", "10m")))
    result = build_basemap(layout, projector, layers=("water",), store=store)
    assert len(result.layers["water"]) == 2


def test_regional_lakes_only_exist_at_10m():
    # Natural Earth ne publie ces suppléments qu'en 10m : aux paliers
    # continent et monde, seuls ocean et lakes sont sélectionnés.
    assert any(d.optional for d in datasets_for("water", "10m"))
    assert not any(d.optional for d in datasets_for("water", "50m"))
    assert not any(d.optional for d in datasets_for("water", "110m"))


def test_paleo_lakes_are_excluded():
    """`lakes_pluvial` et `lakes_historic` sont des lacs disparus."""
    names = {d.name for d in CATALOG}
    assert "ne_10m_lakes_europe" in names
    assert "ne_10m_lakes_pluvial" not in names
    assert "ne_10m_lakes_historic" not in names


def test_lakes_are_not_rank_filtered():
    """`scalerank` monte à 9 dans ne_10m_lakes, au-delà du plafond
    « région » (8) : filtrer les lacs par rang en écartait 35 %."""
    lakes = [d for d in CATALOG if d.layer == "water" and "lakes" in d.name]
    assert lakes
    assert all(d.rank_field is None for d in lakes)


def test_lakes_survive_the_strictest_rank_threshold(store, frame):
    from traceart.basemap.tiers import Tier

    projector, layout = frame
    # `max_rank=0` écarterait tout jeu filtré par rang. Les lacs du
    # fixture portent un `scalerank` de 2 et doivent malgré tout sortir :
    # seule leur taille à l'écran compte.
    strict = Tier("strict", 0.0, "10m", max_rank=0, min_population=0)
    result = build_basemap(
        layout, projector, layers=("water",), store=store, tier=strict
    )
    assert len(result.layers["water"]) == 2


# ------------------------------------------------------------ noms de pays


def test_country_labels_are_produced(store, frame):
    projector, layout = frame
    result = build_basemap(layout, projector, layers=("countries",), store=store)
    kinds = {label.kind for label in result.labels}
    assert kinds == {"country"}
    assert "Grandpays" in [label.text for label in result.labels]


def test_country_name_falls_back_when_translation_missing(store, frame):
    """`NAME_FR` de Natural Earth est vide pour certains territoires."""
    projector, layout = frame
    result = build_basemap(layout, projector, layers=("countries",), store=store)
    assert "Fallbackland" in [label.text for label in result.labels]


def test_sliver_country_is_not_labelled(store, frame):
    """Un pays qui ne mord que le bord du cadre recevrait un label tassé
    contre le bord."""
    projector, layout = frame
    result = build_basemap(layout, projector, layers=("countries",), store=store)
    assert "Liseré" not in [label.text for label in result.labels]


def test_country_labels_sorted_by_visible_area(store, frame):
    projector, layout = frame
    result = build_basemap(layout, projector, layers=("countries",), store=store)
    # Grandpays occupe la majeure partie du cadre : il sort en premier.
    assert result.labels[0].text == "Grandpays"


def test_country_label_sits_inside_the_frame(store, frame):
    """Le centroïde du polygone entier tomberait souvent hors du cadre :
    le centre de la France est loin d'une trace alpine."""
    projector, layout = frame
    result = build_basemap(layout, projector, layers=("countries",), store=store)
    for label in result.labels:
        assert layout.margins.left * 0 <= label.x <= layout.width
        assert 0 <= label.y <= layout.height - layout.margins.bottom


def test_countries_layer_is_selectable():
    from traceart.basemap.catalog import AVAILABLE_LAYERS, DEFAULT_LAYERS

    assert "countries" in AVAILABLE_LAYERS
    # Opt-in : un poster n'a pas toujours besoin des noms de pays.
    assert "countries" not in DEFAULT_LAYERS


def test_countries_have_no_drawn_geometry(store, frame):
    """La couche ne produit que du texte, aucun chemin."""
    projector, layout = frame
    result = build_basemap(layout, projector, layers=("countries",), store=store)
    assert result.layers == {}
    assert result.labels


# ------------------------------------------- cache incomplet, couche par couche


def _drop(store, name):
    """Retire un jeu du cache, comme s'il n'avait jamais été téléchargé."""
    store.layer_path(dataset_by_name(name)).unlink()


@pytest.fixture
def gpx_over_fixture(tmp_path):
    """GPX suivant `TRACK`, donc couvert par les jeux du fixture."""
    from tests.conftest import make_gpx, trkpt

    points = [(3.2, 44.0), (3.35, 44.15), (3.5, 44.3), (3.65, 44.2), (3.8, 44.1)]
    body = "<trk><trkseg>" + "".join(trkpt(lon, lat) for lon, lat in points) + "</trkseg></trk>"
    path = tmp_path / "fixture-area.gpx"
    path.write_text(make_gpx(body), encoding="utf-8")
    return path


def test_one_missing_dataset_drops_only_its_layer(store, gpx_over_fixture):
    """Globalement, un seul jeu manquant emportait tout le fond :
    demander `roads` sans `ne_10m_roads` faisait disparaître jusqu'aux
    frontières et aux noms de pays."""
    from traceart.pipeline import Options, run

    _drop(store, "ne_10m_roads")
    result = run(
        [gpx_over_fixture],
        Options(
            cache_dir=store.root,
            use_osm=False,
            layers=("water", "roads", "borders", "countries"),
        ),
    )
    assert "roads" not in result.svg
    assert "basemap-water" in result.svg
    assert "basemap-borders" in result.svg
    assert "labels-country" in result.svg


def test_note_names_the_dropped_layer(store, gpx_over_fixture):
    from traceart.pipeline import Options, run

    _drop(store, "ne_10m_roads")
    result = run(
        [gpx_over_fixture],
        Options(cache_dir=store.root, use_osm=False, layers=("water", "roads")),
    )
    assert result.basemap_note is not None
    assert "roads" in result.basemap_note
    assert "data fetch" in result.basemap_note


def test_complete_cache_reports_no_note(store, gpx_over_fixture):
    from traceart.pipeline import Options, run

    result = run(
        [gpx_over_fixture],
        Options(cache_dir=store.root, use_osm=False, layers=("water", "borders")),
    )
    assert result.basemap_note is None


def test_on_mode_still_refuses_an_incomplete_cache(store, gpx_over_fixture):
    from traceart.errors import MissingDataError
    from traceart.pipeline import Options, run

    _drop(store, "ne_10m_roads")
    with pytest.raises(MissingDataError, match="roads") as exc:
        run(
            [gpx_over_fixture],
            Options(
                basemap="on",
                cache_dir=store.root,
                use_osm=False,
                layers=("water", "roads"),
            ),
        )
    assert exc.value.layers == ("roads",)


def test_all_layers_missing_falls_back_to_the_bare_trace(gpx_over_fixture, tmp_path):
    from traceart.pipeline import Options, run

    result = run(
        [gpx_over_fixture],
        Options(cache_dir=tmp_path / "vide", use_osm=False, layers=("water", "roads")),
    )
    assert "rien en cache" in result.basemap_note
    # Rien n'a été dessiné (aucun fond), donc aucune provenance à
    # afficher — mais le palier reste connu : un appelant qui veut
    # proposer un téléchargement doit savoir quelle résolution demander.
    assert result.basemap_source is None
    assert result.tier is not None
    assert result.tier.scale == "10m"
    assert set(result.basemap_missing) >= {"ne_10m_ocean", "ne_10m_roads"}


# ------------------------------------------------ extraction vectorisée


def test_polygon_holes_are_drawn(store, frame):
    """`get_rings` doit rendre les anneaux intérieurs, sinon un lac avec
    une île se dessine plein."""
    import shapely

    from traceart.basemap.query import _to_viewbox_paths

    projector, layout = frame
    ring = [(0.0, 0.0), (400.0, 0.0), (400.0, 400.0), (0.0, 400.0), (0.0, 0.0)]
    hole = [(100.0, 100.0), (300.0, 100.0), (300.0, 300.0), (100.0, 300.0), (100.0, 100.0)]
    poly = shapely.Polygon(ring, [hole])
    paths = _to_viewbox_paths(np.asarray([poly], dtype=object), layout, closed=True)
    assert len(paths) == 2, "anneau extérieur + trou"
    assert all(closed for _, closed in paths)


def test_multipolygon_is_exploded(frame):
    import shapely

    from traceart.basemap.query import _to_viewbox_paths

    _projector, layout = frame
    a = [(0.0, 0.0), (300.0, 0.0), (300.0, 300.0), (0.0, 0.0)]
    b = [(500.0, 0.0), (800.0, 0.0), (800.0, 300.0), (500.0, 0.0)]
    multi = shapely.MultiPolygon([(a, []), (b, [])])
    paths = _to_viewbox_paths(np.asarray([multi], dtype=object), layout, closed=True)
    assert len(paths) == 2


def test_geometry_collection_is_flattened(frame):
    """Une intersection avec le cadre peut rendre une GeometryCollection."""
    import shapely

    from traceart.basemap.query import _to_viewbox_paths

    _projector, layout = frame
    collection = shapely.GeometryCollection(
        [
            shapely.LineString([(0.0, 0.0), (400.0, 400.0)]),
            shapely.Polygon([(0.0, 0.0), (300.0, 0.0), (300.0, 300.0), (0.0, 0.0)]),
        ]
    )
    paths = _to_viewbox_paths(np.asarray([collection], dtype=object), layout, closed=False)
    assert len(paths) == 2


def test_empty_and_missing_geometries_are_skipped(frame):
    import shapely

    from traceart.basemap.query import _to_viewbox_paths

    _projector, layout = frame
    geoms = np.asarray(
        [None, shapely.Polygon(), shapely.LineString([(0.0, 0.0), (400.0, 400.0)])],
        dtype=object,
    )
    assert len(_to_viewbox_paths(geoms, layout, closed=False)) == 1


def test_degenerate_ring_is_dropped(frame):
    """Un anneau d'un seul point ne fait pas un chemin."""
    import shapely

    from traceart.basemap.query import _to_viewbox_paths

    _projector, layout = frame
    geoms = np.asarray([shapely.Point(10.0, 10.0)], dtype=object)
    assert _to_viewbox_paths(geoms, layout, closed=False) == []


# ------------------------------------------------ registre des couches


def test_layer_registry_is_the_single_source_of_truth():
    """Le nom d'une couche, son ordre de dessin, son libellé et son
    appartenance aux défauts vivaient dans trois modules : une couche
    `relief` traînait dans l'ordre de dessin et les quatre thèmes sans
    qu'aucun jeu ne l'alimente."""
    from traceart.basemap.catalog import AVAILABLE_LAYERS, CATALOG, DEFAULT_LAYERS
    from traceart.layers import LAYER_LABELS, LAYER_ORDER
    from traceart.render.theme import load_theme

    catalog_layers = {d.layer for d in CATALOG}
    assert catalog_layers == set(AVAILABLE_LAYERS), "jeu sans couche déclarée"
    assert set(DEFAULT_LAYERS) <= set(AVAILABLE_LAYERS)
    assert set(LAYER_ORDER) <= set(AVAILABLE_LAYERS), "couche fantôme dans l'ordre"
    assert set(LAYER_LABELS) == set(AVAILABLE_LAYERS)

    # Chaque couche géométrique doit avoir un style dans le thème de base.
    themed = set(load_theme("light").get("layers", {}))
    assert set(AVAILABLE_LAYERS) <= themed, "couche sans style"
    assert themed <= set(AVAILABLE_LAYERS), "style sans couche"


def test_text_layers_are_excluded_from_the_draw_order():
    """`countries` et `labels` ne produisent aucun chemin : ils sont
    dessinés après la trace, pas dans l'ordre du fond."""
    from traceart.layers import LAYER_ORDER, LAYERS

    text_only = {spec.name for spec in LAYERS if spec.text_only}
    assert text_only == {"countries", "labels"}
    assert text_only.isdisjoint(LAYER_ORDER)


def test_internal_boundaries_drawn_under_national_borders():
    from traceart.layers import LAYER_ORDER

    assert LAYER_ORDER.index("boundaries") < LAYER_ORDER.index("borders")


# ------------------------------------------------ missing / source honnêtes


def test_basemap_result_source_none_when_nothing_drawn(store, frame):
    """Un `tier` connu ne suffit pas à conclure qu'un fond a été rendu :
    il est conservé même sur repli total, pour qu'un appelant sache
    quelle résolution proposer au téléchargement."""
    from traceart.basemap.query import BasemapResult

    empty = BasemapResult(layers={}, labels=[], tier=_tier("local"))
    assert empty.source is None


def test_basemap_result_source_present_when_labels_only(store, frame):
    """Un fond qui ne pose que des labels (aucun chemin `layers`) doit
    quand même annoncer sa provenance."""
    from traceart.basemap.query import BasemapResult
    from traceart.render.label import Label

    with_labels_only = BasemapResult(
        layers={}, labels=[Label(0.0, 0.0, "Test")], tier=_tier("local")
    )
    assert with_labels_only.source == "Natural Earth 10m"


def test_result_exposes_basemap_missing(gpx_over_fixture, tmp_path):
    from traceart.pipeline import Options, run

    result = run(
        [gpx_over_fixture],
        Options(cache_dir=tmp_path / "vide", use_osm=False, layers=("water",)),
    )
    assert "ne_10m_ocean" in result.basemap_missing


def test_basemap_missing_empty_when_cache_complete(store, gpx_over_fixture):
    from traceart.pipeline import Options, run

    result = run(
        [gpx_over_fixture],
        Options(cache_dir=store.root, use_osm=False, layers=("water",)),
    )
    assert result.basemap_missing == ()


def test_basemap_missing_empty_when_basemap_off(gpx_over_fixture, tmp_path):
    from traceart.pipeline import Options, run

    result = run(
        [gpx_over_fixture], Options(cache_dir=tmp_path / "vide", basemap="off")
    )
    assert result.basemap_missing == ()
    assert result.tier is None


def test_missing_data_error_carries_actionable_attributes(gpx_over_fixture, tmp_path):
    from traceart.errors import MissingDataError
    from traceart.pipeline import Options, run

    with pytest.raises(MissingDataError) as exc:
        run(
            [gpx_over_fixture],
            Options(
                cache_dir=tmp_path / "vide",
                use_osm=False,
                basemap="on",
                layers=("water",),
            ),
        )
    assert exc.value.scale == "10m"
    assert "ne_10m_ocean" in exc.value.datasets
    assert exc.value.layers == ("water",)


# ------------------------------------------------------ verrou de cache


def test_fetch_lock_serializes_concurrent_writers(tmp_path):
    """Deux « processus » qui tentent d'écrire le cache en même temps ne
    doivent jamais s'exécuter en même temps — c'est le cas CLI+web sur
    le même cache que le verrou couvre, invisible à un `ThreadJobRunner`
    mono-processus."""
    import threading
    import time

    from traceart.basemap.download import fetch_lock

    order: list[str] = []
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        barrier.wait()
        with fetch_lock(tmp_path):
            order.append(f"{name}-start")
            time.sleep(0.05)
            order.append(f"{name}-end")

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    # Sérialisé : le premier "start" est immédiatement suivi de son
    # propre "end", jamais entrelacé avec l'autre worker.
    assert order[0].endswith("start")
    assert order[1].endswith("end")
    assert order[0][0] == order[1][0]


def test_fetch_lock_creates_the_cache_dir(tmp_path):
    from traceart.basemap.download import fetch_lock

    target = tmp_path / "absent" / "cache"
    with fetch_lock(target):
        pass
    assert target.is_dir()
    assert (target / ".fetch.lock").is_file()
