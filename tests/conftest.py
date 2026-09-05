from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
import shapely

from traceart.basemap.catalog import dataset_by_name
from traceart.basemap.store import Store
from traceart.core.model import Segment, Track
from traceart.core.project import choose_projection, project_track
from traceart.render.layout import fit_layout

DATA = Path(__file__).parent / "data"


def make_gpx(body: str, *, namespaced: bool = True) -> str:
    ns = ' xmlns="http://www.topografix.com/GPX/1/1"' if namespaced else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<gpx version="1.1" creator="test"{ns}>\n{body}\n</gpx>\n'
    )


def trkpt(lon: float, lat: float, ele: float | None = None, time: str | None = None) -> str:
    inner = ""
    if ele is not None:
        inner += f"<ele>{ele}</ele>"
    if time is not None:
        inner += f"<time>{time}</time>"
    return f'<trkpt lat="{lat}" lon="{lon}">{inner}</trkpt>'


def segment(points: list[tuple[float, float]], ele: list[float] | None = None) -> Segment:
    coords = np.asarray(points, dtype=np.float64)
    n = len(coords)
    return Segment(
        coords=coords,
        ele=np.asarray(ele, dtype=np.float64) if ele else np.full(n, math.nan),
        time=np.full(n, math.nan),
    )


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path_factory, monkeypatch):
    """Isole les tests du cache de données de fond de la machine.

    `default_cache_dir()` suit XDG_CACHE_HOME. Sans cette isolation, un
    test qui n'indique pas de `cache_dir` lirait le vrai cache de
    l'utilisateur : la suite passerait ou non selon ce qu'il a
    téléchargé.
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path_factory.mktemp("xdg")))


@pytest.fixture
def straight_track() -> Track:
    """Ligne droite de 11 points : Douglas-Peucker doit n'en garder que 2."""
    pts = [(2.0 + i * 0.01, 45.0) for i in range(11)]
    return Track(segments=[segment(pts)], name="droite")


@pytest.fixture
def gpx_file(tmp_path: Path) -> Path:
    body = "\n".join(
        [
            "<metadata><name>Trace de test</name></metadata>",
            '<wpt lat="44.0" lon="3.0"><name>Parasite</name></wpt>',
            "<trk><name>Ignoré car metadata gagne</name><trkseg>",
            trkpt(3.0, 44.0, 100.0, "2024-05-08T08:00:00Z"),
            trkpt(3.01, 44.005, 140.0, "2024-05-08T08:10:00Z"),
            trkpt(3.02, 44.01, 180.0, "2024-05-08T08:20:00Z"),
            trkpt(3.03, 44.02, 220.0, "2024-05-08T08:30:00Z"),
            "</trkseg><trkseg>",
            trkpt(3.05, 44.03, 240.0, "2024-05-08T09:00:00Z"),
            trkpt(3.06, 44.04, 200.0, "2024-05-08T09:10:00Z"),
            "</trkseg></trk>",
            "<rte>",
            '<rtept lat="44.05" lon="3.07"><ele>190</ele></rtept>',
            '<rtept lat="44.06" lon="3.08"><ele>185</ele></rtept>',
            "</rte>",
        ]
    )
    path = tmp_path / "test.gpx"
    path.write_text(make_gpx(body), encoding="utf-8")
    return path


# ---------------------------------------------------------- fond de carte

# Zone de test : Cévennes, autour de (3.5° E, 44.2° N).
TRACK = Track(segments=[segment([(3.2, 44.0), (3.5, 44.3), (3.8, 44.1)])])


def _tier(name: str):
    from traceart.basemap.tiers import TIERS

    return next(t for t in TIERS if t.name == name)


def _write(store: Store, name: str, geoms, fields: dict[str, np.ndarray], geom_type: str) -> None:
    from pyogrio.raw import write

    store.layers_dir.mkdir(parents=True, exist_ok=True)
    dataset = dataset_by_name(name)
    assert dataset is not None, name
    write(
        str(store.layer_path(dataset)),
        geometry=shapely.to_wkb(geoms),
        field_data=[fields[key] for key in fields],
        fields=np.array(list(fields), dtype=object),
        geometry_type=geom_type,
        crs="EPSG:4326",
        driver="FlatGeobuf",
    )


@pytest.fixture
def store(tmp_path) -> Store:
    """Cache peuplé de jeux 10m synthétiques couvrant la zone de test."""
    store = Store(tmp_path / "cache")

    # Tout doit tomber dans le cadre visible (~3,15–3,85 E / 43,95–44,35 N),
    # sinon la requête par bounding box renvoie une couche vide et le test
    # ne mesure plus rien.
    ocean = shapely.polygons(
        np.asarray([(3.15, 43.9), (3.30, 43.9), (3.30, 44.4), (3.15, 44.4), (3.15, 43.9)])
    )
    _write(store, "ne_10m_ocean", np.asarray([ocean]), {}, "Polygon")

    lake_big = shapely.polygons(
        np.asarray([(3.45, 44.15), (3.55, 44.15), (3.55, 44.25), (3.45, 44.25), (3.45, 44.15)])
    )
    # Lac de 10 m de côté : sous le seuil de visibilité, il ferait un
    # point noir parasite.
    lake_tiny = shapely.polygons(
        np.asarray(
            [(3.60, 44.20), (3.6001, 44.20), (3.6001, 44.2001), (3.60, 44.2001), (3.60, 44.20)]
        )
    )
    _write(
        store,
        "ne_10m_lakes",
        np.asarray([lake_big, lake_tiny]),
        {"scalerank": np.array([2, 2], dtype=np.int32)},
        "Polygon",
    )

    coast = shapely.linestrings([[(3.30, 43.9), (3.30, 44.4)]])
    _write(store, "ne_10m_coastline", coast, {}, "LineString")

    rivers = shapely.linestrings(
        [
            [(3.2, 44.0), (3.8, 44.3)],  # fleuve majeur
            [(3.35, 44.05), (3.45, 44.10)],  # ruisseau
        ]
    )
    _write(
        store,
        "ne_10m_rivers_lake_centerlines",
        rivers,
        {"scalerank": np.array([1, 11], dtype=np.int32)},
        "LineString",
    )

    borders = shapely.linestrings([[(3.70, 43.95), (3.70, 44.35)]])
    _write(store, "ne_10m_admin_0_boundary_lines_land", borders, {}, "LineString")
    _write(store, "ne_10m_admin_1_states_provinces_lines", borders, {}, "LineString")

    roads = shapely.linestrings(
        [[(3.2, 44.05), (3.8, 44.25)], [(3.40, 44.02), (3.50, 44.06)]]
    )
    _write(
        store,
        "ne_10m_roads",
        roads,
        {"scalerank": np.array([3, 12], dtype=np.int32)},
        "LineString",
    )

    # Pays : un grand, un liseré sous le seuil de visibilité, et un
    # troisième sans nom français pour exercer le repli sur NAME.
    def _country_box(x0, x1):
        return shapely.polygons(
            np.asarray([(x0, 43.8), (x1, 43.8), (x1, 44.5), (x0, 44.5), (x0, 43.8)])
        )

    _write(
        store,
        "ne_10m_admin_0_countries",
        np.asarray([_country_box(3.00, 3.70), _country_box(3.70, 3.84), _country_box(3.84, 3.99)]),
        {
            "NAME_FR": np.array(["Grandpays", "", "Liseré"], dtype=object),
            "NAME": np.array(["Bigland", "Fallbackland", "Sliverland"], dtype=object),
        },
        "Polygon",
    )

    cities = shapely.points([(3.5, 44.28), (3.6, 44.05)])
    _write(
        store,
        "ne_10m_populated_places",
        cities,
        {
            "SCALERANK": np.array([2, 8], dtype=np.int32),
            "NAME": np.array(["Grandville", "Petitbourg"], dtype=object),
            "POP_MAX": np.array([500_000, 3_000], dtype=np.int64),
        },
        "Point",
    )
    return store


@pytest.fixture
def frame():
    """Projection + mise en page correspondant à la zone de test."""
    projector = choose_projection([TRACK])
    projected = project_track(TRACK, projector)
    layout = fit_layout(projected.bounds(), long_edge=1000.0, margin=56.0)
    return projector, layout


# ------------------------------------------------------------------- web


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """La suite est hors-ligne par discipline depuis le premier jour ;
    ce filet la rend hors-ligne par construction. Un test qui tenterait
    malgré tout un téléchargement échoue net, plutôt que de dépendre
    (lentement, silencieusement) d'un vrai réseau."""

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("appel réseau tenté pendant les tests")

    monkeypatch.setattr("traceart.basemap.download.download_to", _forbidden)
    # La recherche de lieux (Nominatim) n'utilise pas `download_to` — un
    # appel `urllib.request` qui lui est propre. `web/routes.py` fait
    # `from traceart.web.geocode import search_places` (import par
    # valeur) : patcher seulement `web.geocode.search_places` laisserait
    # la référence que la route appelle réellement intacte. Les deux
    # noms sont donc patchés — celui que le code appelle, et le point
    # d'origine, en défense en profondeur si un futur module importe la
    # fonction directement lui aussi.
    monkeypatch.setattr("traceart.web.geocode.search_places", _forbidden)
    monkeypatch.setattr("traceart.web.routes.search_places", _forbidden)


@pytest.fixture
def web_app(tmp_path, store):
    """Application web sur le cache Natural Earth synthétique du fixture
    `store`, avec un `InlineJobRunner` : les tests n'ont jamais besoin
    d'attendre un thread réel."""
    from traceart.web import create_app
    from traceart.web.jobs import InlineJobRunner
    from traceart.web.settings import WebSettings

    settings = WebSettings(cache_dir=store.root, work_dir=tmp_path / "web-work")
    return create_app(settings, jobs=InlineJobRunner())


@pytest.fixture
def client(web_app):
    from fastapi.testclient import TestClient

    return TestClient(web_app)


@pytest.fixture
def empty_store_app(tmp_path):
    """Même application, mais sur un cache totalement vide — exerce le
    bandeau « données manquantes »."""
    from traceart.web import create_app
    from traceart.web.jobs import InlineJobRunner
    from traceart.web.settings import WebSettings

    settings = WebSettings(
        cache_dir=tmp_path / "cache-vide", work_dir=tmp_path / "web-work"
    )
    return create_app(settings, jobs=InlineJobRunner())


@pytest.fixture
def empty_client(empty_store_app):
    from fastapi.testclient import TestClient

    return TestClient(empty_store_app)
