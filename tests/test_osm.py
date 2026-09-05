"""Tests du Tier B sur des extraits `.osm` XML synthétiques.

Le pilote OSM de GDAL lit indifféremment le XML `.osm` et le binaire
`.osm.pbf` : le chemin de code testé est exactement celui de production,
sans télécharger 155 Mo ni dépendre du réseau.
"""

from __future__ import annotations

import os
import time

import pytest

from tests.conftest import _tier
from traceart.basemap.osm import (
    ADMIN_RANKS,
    CATALOG_MAX_AGE_S,
    OSM_LAYERS,
    PLACE_POPULATION,
    PLACE_RANKS,
    ROAD_RANKS,
    WATERWAY_RANKS,
    OsmError,
    OsmStore,
    geofabrik_url,
    lines_where,
    slugify_region,
)
from traceart.basemap.tiers import choose_tier

# Cévennes / Lozère, autour de (3,7° E ; 44,4° N).
HEADER = '<?xml version="1.0" encoding="UTF-8"?>\n<osm version="0.6" generator="traceart-test">\n'
FOOTER = "</osm>\n"


def _node(nid: int, lon: float, lat: float, tags: dict[str, str] | None = None) -> str:
    inner = "".join(f'<tag k="{k}" v="{v}"/>' for k, v in (tags or {}).items())
    return f'<node id="{nid}" lat="{lat}" lon="{lon}">{inner}</node>\n'


def _way(wid: int, nodes: list[int], tags: dict[str, str]) -> str:
    refs = "".join(f'<nd ref="{n}"/>' for n in nodes)
    inner = "".join(f'<tag k="{k}" v="{v}"/>' for k, v in tags.items())
    return f'<way id="{wid}">{refs}{inner}</way>\n'


def _relation(rid: int, members: list[tuple[str, int, str]], tags: dict[str, str]) -> str:
    inner = "".join(f'<member type="{t}" ref="{r}" role="{role}"/>' for t, r, role in members)
    inner += "".join(f'<tag k="{k}" v="{v}"/>' for k, v in tags.items())
    return f'<relation id="{rid}">{inner}</relation>\n'


@pytest.fixture
def extract(tmp_path):
    """Extrait couvrant 3,0–4,0 E / 43,8–44,6 N.

    Volontairement plus large que le cadre des fixtures partagées : la
    sélection de source exige l'inclusion totale de l'emprise.
    """
    parts = [HEADER]
    # Grille de nœuds pour les lignes.
    for i in range(10):
        parts.append(_node(100 + i, 3.05 + i * 0.10, 43.90 + i * 0.07))
    # Anneau de plan d'eau.
    for i, (lon, lat) in enumerate(
        [(3.60, 44.40), (3.70, 44.40), (3.70, 44.48), (3.60, 44.48)]
    ):
        parts.append(_node(200 + i, lon, lat))
    # Anneau de limite administrative (relation).
    for i, (lon, lat) in enumerate(
        [(3.02, 43.82), (3.98, 43.82), (3.98, 44.58), (3.02, 44.58)]
    ):
        parts.append(_node(300 + i, lon, lat))
    # Villes.
    parts.append(
        _node(
            400,
            3.60,
            44.30,
            {"place": "city", "name": "Grandville", "population": "180000"},
        )
    )
    parts.append(
        _node(
            401,
            3.75,
            44.35,
            {"place": "town", "name": "Bourgville", "population": "22 000"},
        )
    )
    parts.append(_node(402, 3.85, 44.45, {"place": "village", "name": "Petitlieu"}))
    parts.append(_node(403, 3.90, 44.50, {"place": "town", "name": "Sanschiffre"}))
    parts.append(_node(404, 3.95, 44.55, {"place": "hamlet", "name": "Écart"}))
    parts.append(_node(405, 3.55, 44.25, {"place": "city"}))  # sans nom

    # Routes de chaque niveau.
    parts.append(_way(500, [100, 101, 102], {"highway": "motorway", "name": "A75"}))
    parts.append(_way(501, [102, 103, 104], {"highway": "primary", "name": "N88"}))
    parts.append(_way(502, [104, 105, 106], {"highway": "tertiary", "name": "D999"}))
    # Jamais dessinée : rang 14, au-delà de tout palier.
    parts.append(_way(503, [106, 107], {"highway": "residential", "name": "Rue Basse"}))

    # Cours d'eau.
    parts.append(_way(510, [100, 103, 106], {"waterway": "river", "name": "Le Tarn"}))
    parts.append(_way(511, [101, 104], {"waterway": "stream", "name": "Ruisseau"}))
    parts.append(_way(512, [102, 105], {"waterway": "ditch"}))  # jamais dessiné

    # Plans d'eau.
    parts.append(
        _way(520, [200, 201, 202, 203, 200], {"natural": "water", "name": "Lac de Naussac"})
    )
    parts.append(_way(521, [200, 201, 202, 203, 200], {"landuse": "reservoir", "name": "Retenue"}))

    # Limite administrative : relation, comme dans le vrai OSM.
    parts.append(_way(530, [300, 301, 302, 303, 300], {}))
    parts.append(
        _relation(
            600,
            [("way", 530, "outer")],
            {"type": "boundary", "boundary": "administrative", "admin_level": "6"},
        )
    )
    parts.append(FOOTER)

    path = tmp_path / "cevennes-test.osm"
    path.write_text("".join(parts), encoding="utf-8")
    return path


@pytest.fixture
def imported(tmp_path, extract):
    store = OsmStore(tmp_path / "cache")
    region = store.import_extract(extract, slug="cevennes-test")
    return store, region


def test_import_reports_an_increasing_fraction_per_stage(tmp_path, extract):
    """Sans repère fin dans le pilote OSM, la fraction de palier est le
    seul indicateur possible pour une barre de progression — mais elle
    doit au moins avancer, pas rester figée sur tout un import qui peut
    prendre plusieurs dizaines de minutes."""
    store = OsmStore(tmp_path / "cache")
    calls: list[tuple[str, float]] = []
    store.import_extract(
        extract, slug="progress-test", on_progress=lambda s, f: calls.append((s, f))
    )
    assert [stage for stage, _ in calls] == ["lines", "multipolygons", "points"]
    fractions = [f for _, f in calls]
    assert fractions == sorted(fractions)
    assert fractions[0] == 0.0
    assert fractions[-1] < 1.0


# ------------------------------------------------------------------ catalogue


def test_geofabrik_url_from_region_path():
    assert geofabrik_url("europe/france/auvergne") == (
        "https://download.geofabrik.de/europe/france/auvergne-latest.osm.pbf"
    )
    # Un chemin déjà complet est laissé tel quel.
    already_full = geofabrik_url("europe/france/auvergne-latest.osm.pbf")
    assert already_full.endswith("auvergne-latest.osm.pbf")


CATALOG_PAYLOAD = {
    "type": "FeatureCollection",
    "features": [
        {
            "properties": {
                "id": "auvergne",
                "parent": "france",
                "name": "Auvergne",
                "urls": {
                    "pbf": "https://download.geofabrik.de/europe/france/auvergne-latest.osm.pbf"
                },
            }
        },
        {
            "properties": {
                "id": "rhone-alpes",
                "parent": "france",
                "name": "Rhône-Alpes",
                "urls": {
                    "pbf": "https://download.geofabrik.de/europe/france/rhone-alpes-latest.osm.pbf"
                },
            }
        },
        # Publiée en shapefile seul : inutilisable, donc écartée.
        {
            "properties": {
                "id": "sans-pbf",
                "parent": "france",
                "name": "Sans PBF",
                "urls": {"shp": "https://download.geofabrik.de/x-free.shp.zip"},
            }
        },
    ],
}


@pytest.fixture
def catalog_store(tmp_path):
    """`OsmStore` avec un catalogue Geofabrik déjà en cache : la
    résolution n'a alors aucune raison de toucher au réseau."""
    import json

    store = OsmStore(tmp_path / "cache")
    store.root.mkdir(parents=True, exist_ok=True)
    store.catalog_path.write_text(json.dumps(CATALOG_PAYLOAD), encoding="utf-8")
    return store


def test_catalog_skips_regions_without_a_pbf(catalog_store):
    regions = catalog_store.catalog()
    assert set(regions) == {"auvergne", "rhone-alpes"}


def test_resolve_accepts_a_bare_region_name(catalog_store):
    """Le cas qui échouait : `auvergne` seul construisait
    `https://download.geofabrik.de/auvergne-latest.osm.pbf`, inexistant,
    et Geofabrik répondait une page HTML en 200."""
    assert catalog_store.resolve_pbf_url("auvergne") == (
        "https://download.geofabrik.de/europe/france/auvergne-latest.osm.pbf"
    )


def test_resolve_is_case_insensitive_and_accepts_display_names(catalog_store):
    assert catalog_store.resolve_pbf_url("AUVERGNE").endswith("auvergne-latest.osm.pbf")
    assert catalog_store.resolve_pbf_url("Rhône-Alpes").endswith(
        "rhone-alpes-latest.osm.pbf"
    )


def test_resolve_suggests_near_matches_on_a_typo(catalog_store):
    with pytest.raises(OsmError, match="auvergne") as exc:
        catalog_store.resolve_pbf_url("auvergnne")
    assert "inconnue" in str(exc.value)


def test_resolve_passes_an_explicit_path_through_without_the_catalog(tmp_path):
    """Un chemin complet reste l'usage documenté, et doit fonctionner
    sans catalogue en cache — donc sans réseau."""
    store = OsmStore(tmp_path / "vide")
    assert not store.catalog_path.exists()
    assert store.resolve_pbf_url("europe/france/auvergne") == (
        "https://download.geofabrik.de/europe/france/auvergne-latest.osm.pbf"
    )


def test_resolve_falls_back_on_a_stale_catalog(catalog_store, monkeypatch):
    """Le catalogue ne bouge qu'à la création ou la fusion d'une région :
    une copie périmée vaut mieux qu'un échec."""
    from traceart.basemap.download import DownloadError

    old = time.time() - CATALOG_MAX_AGE_S - 1
    os.utime(catalog_store.catalog_path, (old, old))

    def failing(*_args, **_kwargs):
        raise DownloadError("réseau coupé (test)")

    monkeypatch.setattr("traceart.basemap.osm.download_to", failing)
    assert catalog_store.resolve_pbf_url("auvergne").endswith("auvergne-latest.osm.pbf")


def test_resolve_reports_a_useful_error_without_catalog_nor_network(tmp_path, monkeypatch):
    from traceart.basemap.download import DownloadError

    def failing(*_args, **_kwargs):
        raise DownloadError("réseau coupé (test)")

    monkeypatch.setattr("traceart.basemap.osm.download_to", failing)
    store = OsmStore(tmp_path / "vide")
    with pytest.raises(OsmError, match="chemin complet"):
        store.resolve_pbf_url("auvergne")


def test_slugify_region():
    assert slugify_region("europe/france/languedoc-roussillon") == (
        "europe-france-languedoc-roussillon"
    )
    assert slugify_region("  Rhône Alpes  ") == "rh-ne-alpes"


def test_ranks_align_with_tier_thresholds():
    """Les rangs doivent tomber de part et d'autre des seuils 8 (région)
    et 12 (local), sinon les paliers de zoom ne discriminent rien."""
    assert ROAD_RANKS["motorway"] < ROAD_RANKS["primary"] < ROAD_RANKS["tertiary"]
    assert ROAD_RANKS["secondary"] <= 8 < ROAD_RANKS["tertiary"] <= 12
    assert WATERWAY_RANKS["river"] <= 8 < WATERWAY_RANKS["stream"] <= 12
    # Département (admin_level 6) visible en région, commune (8) en local.
    assert ADMIN_RANKS["6"] <= 8 < ADMIN_RANKS["8"] <= 12
    assert PLACE_RANKS["town"] <= 8 < PLACE_RANKS["village"] <= 12


def test_sql_filter_excludes_undrawable_tags():
    """Le filtre SQL est dérivé des tables de rangs : ce qui n'a pas de
    rang dessinable n'est jamais lu, ce qui allège l'import d'autant."""
    where = lines_where()
    assert "'motorway'" in where and "'tertiary'" in where
    assert "'residential'" not in where
    assert "'unclassified'" not in where
    assert "'river'" in where and "'stream'" in where
    assert "'ditch'" not in where


def test_no_coastline_layer():
    """La côte reste Natural Earth : OSM ne fournit pas de polygone
    océan, et une côte OSM détaillée sur un océan NE plus grossier
    laisserait voir le décalage."""
    assert "coastline" not in {layer.name for layer in OSM_LAYERS}


# --------------------------------------------------------------------- import


def test_import_produces_expected_layers(imported):
    _store, region = imported
    assert set(region.counts) == {"water", "rivers", "roads", "boundaries", "labels"}
    assert region.features > 0


def test_undrawable_tags_are_not_imported(imported):
    _store, region = imported
    # 3 routes retenues sur 4 (residential écartée), 2 cours d'eau sur 3.
    assert region.counts["roads"] == 3
    assert region.counts["rivers"] == 2


def test_water_accepts_natural_and_landuse(imported):
    _store, region = imported
    # natural=water ET landuse=reservoir.
    assert region.counts["water"] == 2


def test_boundary_relation_is_assembled(imported):
    _store, region = imported
    assert region.counts["boundaries"] == 1


def test_unnamed_place_is_not_a_label(imported):
    _store, region = imported
    # 5 lieux nommés sur 6 ; le hamlet est hors des types acceptés.
    assert region.counts["labels"] == 4


def test_region_bounds_cover_the_extract(imported):
    _store, region = imported
    assert region.covers((3.60, 44.30, 3.90, 44.50))
    assert not region.covers((0.0, 40.0, 10.0, 50.0))


def test_index_survives_a_reload(tmp_path, extract):
    store = OsmStore(tmp_path / "cache")
    original = store.import_extract(extract, slug="reload", sha256="abc123")
    reloaded = OsmStore(tmp_path / "cache").read_index()["reload"]
    assert reloaded.bounds == pytest.approx(original.bounds)
    assert reloaded.sha256 == "abc123"
    assert reloaded.counts == original.counts


def test_index_is_sorted_for_reproducibility(tmp_path, extract):
    store = OsmStore(tmp_path / "cache")
    store.import_extract(extract, slug="zebre")
    store.import_extract(extract, slug="alpha")
    text = store.index_path.read_text(encoding="utf-8")
    assert text.index('"alpha"') < text.index('"zebre"')


def test_corrupt_index_reported(tmp_path):
    store = OsmStore(tmp_path / "cache")
    store.root.mkdir(parents=True, exist_ok=True)
    store.index_path.write_text("{ pas du json", encoding="utf-8")
    with pytest.raises(OsmError, match="index illisible"):
        store.read_index()


def test_missing_extract_reported(tmp_path):
    store = OsmStore(tmp_path / "cache")
    with pytest.raises(OsmError, match="introuvable"):
        store.import_extract(tmp_path / "absent.osm.pbf")


def test_remove_region(imported):
    store, region = imported
    assert store.remove(region.slug)
    assert not store.region_dir(region.slug).exists()
    assert store.regions() == []
    assert not store.remove(region.slug)


def test_smallest_covering_region_wins(tmp_path, extract):
    """À couverture égale, l'extrait le plus serré est le plus détaillé."""
    store = OsmStore(tmp_path / "cache")
    tight = store.import_extract(extract, slug="serre")

    wide_source = tmp_path / "wide.osm"
    wide_source.write_text(
        HEADER
        + _node(1, 0.0, 40.0)
        + _node(2, 10.0, 50.0)
        + _way(3, [1, 2], {"highway": "motorway"})
        + FOOTER,
        encoding="utf-8",
    )
    store.import_extract(wide_source, slug="large")

    bbox = (3.60, 44.30, 3.90, 44.50)
    assert store.covering(bbox).slug == tight.slug
    # Hors de l'extrait serré, seul le large convient.
    assert store.covering((5.0, 41.0, 6.0, 42.0)).slug == "large"
    # Couvert par aucun.
    assert store.covering((-50.0, -50.0, 50.0, 50.0)) is None


def test_population_falls_back_to_place_type(imported):
    """Le tag `population` manque souvent en OSM. Sans repli, filtrer par
    population supprimerait toute commune non renseignée."""
    from pyogrio.raw import read

    store, region = imported
    meta, _f, wkb, fd = read(
        str(store.layer_path(region.slug, "labels")), columns=["name", "population"]
    )
    fields = [str(f) for f in meta["fields"]]
    rows = dict(
        zip(
            [str(v) for v in fd[fields.index("name")]],
            [float(v) for v in fd[fields.index("population")]],
            strict=True,
        )
    )
    assert rows["Grandville"] == 180_000
    # « 22 000 » : le texte libre est nettoyé.
    assert rows["Bourgville"] == 22_000
    # Tag absent : repli sur le type de lieu.
    assert rows["Petitlieu"] == PLACE_POPULATION["village"]
    assert rows["Sanschiffre"] == PLACE_POPULATION["town"]


def test_imported_geometry_is_wgs84(imported):
    from pyogrio import read_info

    store, region = imported
    info = read_info(str(store.layer_path(region.slug, "roads")))
    assert info["crs"] == "EPSG:4326"


def test_reimport_replaces_previous_layers(tmp_path, extract):
    store = OsmStore(tmp_path / "cache")
    store.import_extract(extract, slug="idem")
    # Un second import du même extrait ne doit pas cumuler les entités.
    again = store.import_extract(extract, slug="idem")
    assert again.counts["roads"] == 3
    assert len(store.regions()) == 1


# ----------------------------------------------------- sélection de la source


@pytest.fixture
def both(imported, store, frame):
    """Cache Natural Earth + extrait OSM couvrant le même cadre."""
    osm_store, region = imported
    projector, layout = frame
    return osm_store, region, store, projector, layout


def _build(both, **kwargs):
    from traceart.basemap.query import build_basemap

    osm_store, _region, ne_store, projector, layout = both
    options = {
        "layers": ("water", "coastline", "rivers", "roads", "boundaries", "labels"),
        "store": ne_store,
        "osm_store": osm_store,
    }
    options.update(kwargs)
    return build_basemap(layout, projector, **options)


def test_osm_covers_the_shared_frame(both):
    osm_store, _region, _ne, projector, layout = both
    from traceart.basemap.query import visible_bounds_wgs84

    assert osm_store.covering(visible_bounds_wgs84(layout, projector)) is not None


def test_osm_replaces_the_richer_layers(both):
    result = _build(both)
    assert set(result.osm_layers) == {"water", "rivers", "roads", "boundaries", "labels"}
    assert result.osm_region == "cevennes-test"


def test_coastline_stays_natural_earth(both):
    """OSM ne fournit pas de polygone océan : garder sa côte détaillée
    au-dessus d'un océan Natural Earth ferait voir le décalage."""
    result = _build(both)
    assert "coastline" not in result.osm_layers
    assert result.layers["coastline"]


def test_ocean_survives_under_osm_water(both):
    """`keep_under_osm` : OSM apporte les plans d'eau, pas l'océan."""
    from traceart.basemap.query import osm_region_for, planned_datasets, visible_bounds_wgs84

    osm_store, _region, ne_store, projector, layout = both
    bbox = visible_bounds_wgs84(layout, projector)
    tier = choose_tier(bbox)
    region = osm_region_for(osm_store, bbox, tier)
    planned = planned_datasets(
        ("water",), tier=tier, store=ne_store, osm_store=osm_store, region=region
    )
    names = {d.name for d in planned}
    assert names == {"ne_10m_ocean"}, "seul l'océan doit rester sous OSM"


def test_natural_earth_roads_not_required_when_osm_provides_them(both):
    """Le cache NE ne doit pas être exigé pour une couche qu'OSM fournit :
    sinon l'outil réclame `ne_10m_roads` pour une carte dont les routes
    viennent d'OSM."""
    from traceart.basemap.query import osm_region_for, planned_datasets, visible_bounds_wgs84

    osm_store, _region, ne_store, projector, layout = both
    bbox = visible_bounds_wgs84(layout, projector)
    tier = choose_tier(bbox)
    region = osm_region_for(osm_store, bbox, tier)

    with_osm = planned_datasets(
        ("roads",), tier=tier, store=ne_store, osm_store=osm_store, region=region
    )
    without = planned_datasets(("roads",), tier=tier, store=ne_store)
    assert with_osm == []
    assert {d.name for d in without} == {"ne_10m_roads"}


def test_osm_ignored_at_coarse_tiers(both):
    """À l'échelle d'un continent, le volume OSM est absurde et Natural
    Earth est mieux généralisé."""
    from traceart.basemap.query import osm_region_for, visible_bounds_wgs84

    osm_store, _region, _ne, projector, layout = both
    bbox = visible_bounds_wgs84(layout, projector)
    assert osm_region_for(osm_store, bbox, _tier("local")) is not None
    for name in ("continent", "monde"):
        assert osm_region_for(osm_store, bbox, _tier(name)) is None


def test_osm_ignored_when_region_does_not_cover(store, frame, tmp_path):
    from traceart.basemap.query import build_basemap

    projector, layout = frame
    empty = OsmStore(tmp_path / "vide")
    result = build_basemap(
        layout, projector, layers=("rivers",), store=store, osm_store=empty
    )
    assert result.osm_layers == ()
    assert result.osm_region is None


def test_osm_can_be_switched_off(both):
    result = _build(both, osm_store=None)
    assert result.osm_layers == ()


def test_source_string_names_the_provenance(both):
    result = _build(both)
    assert result.source.startswith("OSM cevennes-test (")
    assert "Natural Earth 10m" in result.source

    ne_only = _build(both, osm_store=None)
    assert ne_only.source == "Natural Earth 10m"


def test_osm_rank_threshold_is_stricter_than_natural_earth(both):
    """Un extrait régional est deux ordres de grandeur plus dense : au
    même plafond, on obtiendrait chaque ruisseau et chaque route
    communale."""
    for tier in (_tier("région"), _tier("local")):
        assert tier.osm_max_rank < tier.max_rank


def test_osm_population_threshold_is_lower_than_natural_earth(both):
    """`POP_MAX` de Natural Earth compte l'agglomération, le tag OSM
    `population` la commune : Bergame vaut 500 000 chez l'un et 120 000
    chez l'autre. Au seuil de Natural Earth, l'arc alpin n'aurait qu'un
    seul label."""
    for name in ("monde", "continent", "région", "local"):
        tier = _tier(name)
        assert tier.osm_min_population < tier.min_population


def test_medium_town_labelled_from_osm_but_not_natural_earth(store, imported, frame):
    """Un chef-lieu de 30 000 habitants mérite un label sur un poster ;
    le seuil Natural Earth l'écarterait."""
    from traceart.basemap.query import build_basemap

    osm_store, _region = imported
    projector, layout = frame
    regional = _tier("région")
    assert regional.osm_min_population >= 25_000
    assert regional.min_population > 100_000

    result = build_basemap(
        layout,
        projector,
        layers=("labels",),
        store=store,
        osm_store=osm_store,
        tier=regional,
    )
    # Grandville (180 000, place=city) passe ; Bourgville (22 000,
    # place=town, rang 6) est écartée par le plafond de rang OSM.
    assert [lbl.text for lbl in result.labels] == ["Grandville"]


def test_tertiary_roads_dropped_at_local_tier(both):
    """Palier local en OSM : réseau primaire et secondaire, pas tertiaire."""
    result = _build(both, layers=("roads",))
    # 3 routes importées (motorway, primary, tertiary) ; la tertiaire
    # (rang 10) passe au-dessus du plafond OSM local (8).
    assert len(result.layers["roads"]) == 2


def test_streams_dropped_at_local_tier(both):
    result = _build(both, layers=("rivers",))
    # Fleuve (rang 3) gardé, ruisseau (rang 10) écarté.
    assert len(result.layers["rivers"]) == 1


# --------------------------------------------------- découpe à l'import


def test_bbox_restricts_the_import(tmp_path, extract):
    """Sans découpe, les entités retenues arrivent en une seule fois en
    mémoire — l'arc alpin entier en compte plus d'un million."""
    store = OsmStore(tmp_path / "cache")
    whole = store.import_extract(extract, slug="entier")
    # Pointe ouest seulement : la grille de nœuds va de 3,05 à 3,95, et
    # seule l'autoroute (3,05 → 3,25) intersecte cette emprise.
    clipped = store.import_extract(
        extract, slug="ouest", bbox=(3.00, 43.80, 3.20, 44.60)
    )
    assert whole.counts["roads"] == 3
    assert clipped.counts["roads"] == 1
    assert clipped.features < whole.features
    # Le plan d'eau (3,60 → 3,70) est hors emprise.
    assert clipped.counts.get("water", 0) == 0


def test_bbox_bounds_the_declared_coverage(tmp_path, extract):
    """Le filtre spatial de GDAL renvoie les entités qui *intersectent*
    l'emprise : une ligne qui la traverse s'étend au-delà. L'union brute
    ferait croire à une couverture inexistante."""
    store = OsmStore(tmp_path / "cache")
    clip = (3.00, 43.80, 3.45, 44.60)
    region = store.import_extract(extract, slug="borne", bbox=clip)
    assert region.bounds[0] >= clip[0]
    assert region.bounds[1] >= clip[1]
    assert region.bounds[2] <= clip[2]
    assert region.bounds[3] <= clip[3]
    # Et donc pas de fausse promesse de couverture à l'est.
    assert not region.covers((3.60, 44.30, 3.90, 44.50))


def test_bbox_around_pads_from_the_longest_axis():
    """Le petit axe est celui que le cadre de sortie étire le plus : le
    padder de sa propre étendue laisse le fond trop court."""
    padded = OsmStore.bbox_around((3.0, 44.0, 4.0, 44.4), pad_ratio=0.25)
    # Grand axe 1,0° → 0,25° de marge sur les deux axes.
    assert padded == pytest.approx((2.75, 43.75, 4.25, 44.65))


def test_bbox_around_covers_the_render_frame():
    """Cas réel : la trace ALPES/PARTIE_2 fait 2,93° sur 0,57°, et son
    cadre de sortie monte 0,19° au-dessus de la trace."""
    from tests.conftest import segment
    from traceart.basemap.query import visible_bounds_wgs84
    from traceart.core.model import Track
    from traceart.core.project import choose_projection, project_track
    from traceart.render.layout import fit_layout

    track = Track(segments=[segment([(7.144, 45.474), (8.6, 46.043), (10.078, 45.6)])])
    projector = choose_projection([track])
    layout = fit_layout(
        project_track(track, projector).bounds(), long_edge=1000.0, margin=56.0
    )
    frame = visible_bounds_wgs84(layout, projector)
    box = OsmStore.bbox_around(track.bounds())
    assert box[0] <= frame[0] and box[1] <= frame[1]
    assert box[2] >= frame[2] and box[3] >= frame[3]


def test_bbox_around_has_a_floor_for_tiny_traces():
    """Une trace de 2 km ne doit pas produire une emprise dégénérée."""
    padded = OsmStore.bbox_around((3.0, 44.0, 3.01, 44.01))
    assert padded[2] - padded[0] >= 0.1
    assert padded[3] - padded[1] >= 0.1


def test_bbox_around_stays_within_world_bounds():
    padded = OsmStore.bbox_around((-179.0, -89.0, 179.0, 89.0))
    assert padded[0] >= -180.0 and padded[1] >= -90.0
    assert padded[2] <= 180.0 and padded[3] <= 90.0
