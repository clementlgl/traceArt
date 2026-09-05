"""Lecture, filtrage et mise en page des géométries de fond.

Chaîne : bounding box de la vue → requête FlatGeobuf indexée → filtrage
par importance → reprojection → découpe au cadre → coordonnées viewBox.

La découpe se fait dans l'espace projeté, avant conversion en unités
viewBox : une côte mondiale réduite au rectangle visible passe de
millions de points à quelques centaines.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import shapely
from pyproj import Transformer

from traceart.basemap.catalog import AVAILABLE_LAYERS, Dataset, datasets_for
from traceart.basemap.osm import OsmRegion, OsmStore, layer_by_name
from traceart.basemap.store import Store, StoreError
from traceart.basemap.tiers import Tier, choose_tier
from traceart.core.project import Projector
from traceart.errors import UnavailableError
from traceart.render.label import COUNTRY, Label
from traceart.render.layout import Layout

# Marge de sécurité autour de la bounding box demandée, en fraction de
# l'étendue : la reprojection courbe les bords, une requête au ras du
# cadre laisserait des trous dans les coins.
_FETCH_PAD_RATIO = 0.08
# Points par bord lors de la densification du cadre : 4 coins ne
# suffisent pas à cerner l'emprise réelle d'une projection conique.
_EDGE_SAMPLES = 32
# Étendue minimale, en unités viewBox (grand côté 1000), pour qu'une
# géométrie apporte quelque chose au dessin. Une surface a besoin de plus
# qu'une ligne pour se lire comme une forme : en dessous, une mare
# devient une moucheture. Un extrait OSM en compte des milliers.
_MIN_AREA_EXTENT = 2.0
_MIN_LINE_EXTENT = 1.0

# Couches où un extrait OSM remplace Natural Earth : il est
# systématiquement plus riche, les garder toutes deux doublerait les
# traits.
# `borders`, `coastline` et `countries` en sont absents : la structure
# de la carte reste sur Natural Earth, complet et généralisé, là où OSM
# apporte le détail.
OSM_REPLACES = frozenset({"rivers", "roads", "boundaries", "labels"})
# Couches où OSM s'ajoute à Natural Earth. Seul l'océan survit dessous
# (`Dataset.keep_under_osm`) : OSM n'a pas de polygone océan.
OSM_ADDS_TO = frozenset({"water"})
# OSM n'est consulté qu'aux paliers 10m (région, local). À l'échelle d'un
# continent, son volume est absurde et Natural Earth est mieux généralisé.
OSM_SCALES = frozenset({"10m"})


# Chemin prêt à dessiner : coordonnées viewBox et fermeture. Défini ici
# plutôt qu'importé du renderer : le fond ne doit pas dépendre du rendu.
Geometry = tuple[np.ndarray, bool]

# Identifiants de type shapely, pour éviter les littéraux nus.
_GEOM_POINT = 0
_GEOM_LINEAR = (1, 2)  # LineString, LinearRing
_GEOM_POLYGON = 3
_GEOM_COLLECTION = 7

# Part minimale du cadre qu'un pays doit occuper pour mériter son nom.
# Sans ce seuil, un pays qui ne mord que le coin de la carte reçoit un
# label tassé contre le bord.
_MIN_COUNTRY_SHARE = 0.03


class BasemapError(UnavailableError):
    """Couche demandée inconnue, ou cache incomplet."""


@dataclass(frozen=True, slots=True)
class BasemapResult:
    """Sortie de `build_basemap`, avec la provenance des géométries."""

    layers: dict[str, list[Geometry]]
    labels: list[Label]
    tier: Tier | None
    osm_region: str | None = None
    osm_layers: tuple[str, ...] = ()
    # Ce qui a été laissé de côté, à remonter à l'utilisateur. Rempli par
    # l'appelant, qui seul sait ce qu'il a retiré de la demande.
    note: str | None = None
    # Noms des jeux Natural Earth absents du cache, quand c'est la cause
    # du retrait. Sans ça, un appelant qui veut proposer un téléchargement
    # (l'interface web, par exemple) devrait analyser la phrase de `note`
    # pour en extraire les noms de jeux — fragile et non contractuel.
    missing: tuple[str, ...] = ()
    # Vrai si le palier justifie un extrait OSM (`OSM_SCALES`) et que
    # `--osm` est actif, mais qu'aucun extrait importé ne couvre
    # l'emprise — silencieux par conception (Natural Earth prend le
    # relais), mais un appelant peut s'en servir pour proposer le
    # téléchargement plutôt que de laisser l'absence passer inaperçue.
    osm_missing: bool = False

    @property
    def source(self) -> str | None:
        """Provenance lisible, pour le rapport CLI. None si aucun fond.

        Le `tier` seul ne suffit pas à conclure qu'un fond a été dessiné :
        il est désormais conservé même quand `missing_datasets` a tout
        écarté (pour qu'un appelant sache quelle résolution proposer au
        téléchargement), auquel cas `layers` et `labels` sont vides.
        """
        if self.tier is None or not (self.layers or self.labels):
            return None
        base = f"Natural Earth {self.tier.scale}"
        if not self.osm_layers:
            return base
        return f"OSM {self.osm_region} ({', '.join(self.osm_layers)}) + {base}"


def _field_index(fields: np.ndarray, name: str | None) -> int | None:
    """Index d'un champ, insensible à la casse.

    Natural Earth mélange les conventions : `scalerank` en minuscules
    dans les couches physiques, `SCALERANK` en majuscules dans les
    couches culturelles.
    """
    if name is None:
        return None
    lowered = [str(f).lower() for f in fields]
    target = name.lower()
    return lowered.index(target) if target in lowered else None


def frame_rect(layout: Layout, *, bleed: bool) -> tuple[float, float, float, float]:
    """Rectangle du fond en unités viewBox : (left, top, right, bottom).

    Le bas s'arrête au bandeau d'annotations quand il y en a un, y compris
    à fond perdu : sinon un plan d'eau se peint par-dessus le titre et le
    profil. Sans annotations, `margins.bottom` n'est qu'une marge de page
    ordinaire, identique aux trois autres côtés : rien à protéger, le bas
    doit alors bleeder comme eux — sinon le fond s'arrête net avant le
    bord de l'image sur ce seul côté.
    Ce rectangle sert à la fois à calculer l'emprise à charger et à
    découper les géométries — les deux doivent coïncider exactement.
    """
    if bleed:
        annotation_band = layout.margins.bottom - layout.margins.top
        bottom = layout.height - layout.margins.bottom if annotation_band > 0 else layout.height
        return 0.0, 0.0, layout.width, bottom
    return (
        layout.margins.left,
        layout.margins.top,
        layout.width - layout.margins.right,
        layout.height - layout.margins.bottom,
    )


def visible_bounds_wgs84(
    layout: Layout, projector: Projector, *, bleed: bool = True
) -> tuple[float, float, float, float]:
    """Emprise WGS84 réellement visible dans le cadre.

    On part du rectangle de sortie (pas de la bounding box de la trace) :
    avec `--aspect square` ou un fond à fond perdu, le cadre découvre du
    terrain que la trace ne couvre pas.
    """
    left, top, right, bottom = frame_rect(layout, bleed=bleed)

    xs = np.linspace(left, right, _EDGE_SAMPLES)
    ys = np.linspace(top, bottom, _EDGE_SAMPLES)
    border = np.concatenate(
        [
            np.column_stack([xs, np.full_like(xs, top)]),
            np.column_stack([xs, np.full_like(xs, bottom)]),
            np.column_stack([np.full_like(ys, left), ys]),
            np.column_stack([np.full_like(ys, right), ys]),
        ]
    )
    projected = layout.invert(border)

    inverse = Transformer.from_crs(projector.crs, "EPSG:4326", always_xy=True)
    lon, lat = inverse.transform(projected[:, 0], projected[:, 1])
    lon = np.asarray(lon, dtype=np.float64)
    lat = np.asarray(lat, dtype=np.float64)
    finite = np.isfinite(lon) & np.isfinite(lat)
    if not finite.any():
        raise BasemapError("cadre non inversible dans la projection choisie")
    lon, lat = lon[finite], lat[finite]

    pad_lon = max((lon.max() - lon.min()) * _FETCH_PAD_RATIO, 0.01)
    pad_lat = max((lat.max() - lat.min()) * _FETCH_PAD_RATIO, 0.01)
    return (
        max(float(lon.min()) - pad_lon, -180.0),
        max(float(lat.min()) - pad_lat, -90.0),
        min(float(lon.max()) + pad_lon, 180.0),
        min(float(lat.max()) + pad_lat, 90.0),
    )


def _label_texts(
    fields: np.ndarray,
    field_data: list[np.ndarray],
    keep: np.ndarray,
    label_field: str | None,
    fallback_field: str | None,
) -> list[str]:
    """Textes de label, avec repli par entité sur un second champ.

    Les traductions de Natural Earth ne couvrent pas tous les
    territoires : `NAME_FR` est parfois vide là où `NAME` est renseigné.
    """
    primary_idx = _field_index(fields, label_field)
    if primary_idx is None:
        return []
    primary = np.asarray(field_data[primary_idx], dtype=object)[keep]

    fallback_idx = _field_index(fields, fallback_field)
    fallback = (
        np.asarray(field_data[fallback_idx], dtype=object)[keep]
        if fallback_idx is not None
        else None
    )

    out: list[str] = []
    for index, value in enumerate(primary):
        text = "" if value is None else str(value).strip()
        if not text and fallback is not None and fallback[index] is not None:
            text = str(fallback[index]).strip()
        out.append(text)
    return out


def _read_features(
    path: Path,
    *,
    bbox: tuple[float, float, float, float],
    max_rank: int,
    min_population: int,
    min_rank: int = 0,
    rank_field: str | None = None,
    label_field: str | None = None,
    label_fallback_field: str | None = None,
    population_field: str | None = None,
) -> tuple[np.ndarray, list[str], np.ndarray | None]:
    """Lit un FlatGeobuf et filtre par importance : (geoms, labels, populations).

    Même code pour Natural Earth et pour OSM : l'import OSM écrit un rang
    de notoriété synthétique dans la colonne `rank`, donc l'aval n'a pas
    à savoir d'où viennent les géométries.
    """
    from pyogrio.raw import read

    columns = [
        c
        for c in (rank_field, label_field, label_fallback_field, population_field)
        if c
    ]
    meta, _fids, wkb, field_data = read(
        str(path), bbox=bbox, columns=columns or None, read_geometry=True
    )
    if wkb is None or len(wkb) == 0:
        return np.empty(0, dtype=object), [], None

    fields = meta["fields"]
    keep = np.ones(len(wkb), dtype=bool)

    rank_idx = _field_index(fields, rank_field)
    if rank_idx is not None:
        ranks = np.asarray(field_data[rank_idx], dtype=np.float64)
        # Un rang manquant vaut « important » : mieux vaut dessiner de
        # trop que de trouer une côte.
        ranks = np.nan_to_num(ranks, nan=0.0)
        keep &= (ranks <= max_rank) & (ranks >= min_rank)

    pop_idx = _field_index(fields, population_field)
    populations = None
    if pop_idx is not None:
        populations = np.asarray(field_data[pop_idx], dtype=np.float64)
        # Population inconnue = on garde : en OSM le tag manque souvent,
        # et l'import a déjà posé une valeur de repli par type de lieu.
        keep &= ~np.isfinite(populations) | (populations >= min_population)

    labels = _label_texts(fields, field_data, keep, label_field, label_fallback_field)

    geoms = shapely.from_wkb(wkb[keep])
    if populations is not None:
        populations = populations[keep]
    return geoms, labels, populations


def _read_dataset(
    store: Store, dataset: Dataset, bbox: tuple[float, float, float, float], tier: Tier
) -> tuple[np.ndarray, list[str], np.ndarray | None]:
    """Jeu Natural Earth du cache."""
    path = store.layer_path(dataset)
    if not path.is_file():
        raise StoreError(f"{dataset.name} : absent du cache")
    return _read_features(
        path,
        bbox=bbox,
        max_rank=tier.max_rank,
        min_population=tier.min_population,
        rank_field=dataset.rank_field,
        label_field=dataset.label_field,
        label_fallback_field=dataset.label_fallback_field,
        population_field=dataset.population_field,
    )


def _read_osm(
    osm_store: OsmStore,
    region: OsmRegion,
    layer_name: str,
    bbox: tuple[float, float, float, float],
    tier: Tier,
) -> tuple[np.ndarray, list[str], np.ndarray | None]:
    """Couche d'un extrait OSM importé."""
    layer = layer_by_name(layer_name)
    if layer is None:
        raise BasemapError(f"couche OSM inconnue : {layer_name}")
    return _read_features(
        osm_store.layer_path(region.slug, layer_name),
        bbox=bbox,
        # Seuils OSM : plus sévère sur le rang, plus permissif sur la
        # population, les deux sources ne comptant pas la même chose.
        max_rank=tier.osm_max_rank,
        min_rank=layer.rank_min,
        min_population=tier.osm_min_population,
        rank_field=layer.rank_field,
        label_field=layer.label_field,
        population_field=layer.population_field,
    )


def _to_viewbox_paths(
    geoms: np.ndarray, layout: Layout, closed: bool
) -> list[Geometry]:
    """Éclate les géométries en chemins de coordonnées viewBox.

    Entièrement vectorisé : `get_parts` explose les Multi*, `get_rings`
    sort les anneaux extérieurs et intérieurs, `get_coordinates` rend
    toutes les coordonnées en un appel, et la mise en page s'applique une
    seule fois. La version en boucle exécutait plus de trente mille
    appels shapely scalaires et autant de micro-opérations numpy, pour un
    coût presque entièrement d'overhead.
    """
    if len(geoms) == 0:
        return []

    parts = shapely.get_parts(geoms)
    # Une intersection peut rendre des GeometryCollection : une seconde
    # passe suffit, shapely n'en produit pas d'imbriquées plus profond.
    if len(parts) and (shapely.get_type_id(parts) == _GEOM_COLLECTION).any():
        parts = shapely.get_parts(parts)
    if len(parts) == 0:
        return []

    kinds = shapely.get_type_id(parts)
    pieces = [shapely.get_rings(parts[kinds == _GEOM_POLYGON])]
    pieces.append(parts[np.isin(kinds, _GEOM_LINEAR)])
    drawable = np.concatenate(pieces)
    if len(drawable) == 0:
        return []

    coords, index = shapely.get_coordinates(drawable, return_index=True)
    if len(coords) == 0:
        return []
    placed = layout.apply(coords)

    # Frontières entre géométries : `index` est croissant par
    # construction, un changement de valeur ouvre un nouvel anneau.
    cuts = np.flatnonzero(np.diff(index)) + 1
    minimum = _MIN_AREA_EXTENT if closed else _MIN_LINE_EXTENT

    paths: list[Geometry] = []
    for ring in np.split(placed, cuts):
        if len(ring) < 2:
            continue
        extent = max(
            ring[:, 0].max() - ring[:, 0].min(),
            ring[:, 1].max() - ring[:, 1].min(),
        )
        if extent >= minimum:
            paths.append((ring, closed))
    return paths


def build_basemap(
    layout: Layout,
    projector: Projector,
    *,
    layers: tuple[str, ...],
    store: Store,
    bleed: bool = True,
    tier: Tier | None = None,
    osm_store: OsmStore | None = None,
) -> BasemapResult:
    """Construit les chemins de fond et les labels, en unités viewBox.

    Les couches suivent la nomenclature des thèmes, les labels sont des
    `(x, y, texte)`. Si un extrait OSM importé couvre entièrement
    l'emprise et que le palier est en 10m, il prend le relais de Natural
    Earth sur les couches où il est plus riche.
    """
    unknown = [name for name in layers if name not in AVAILABLE_LAYERS]
    if unknown:
        raise BasemapError(
            f"couche(s) inconnue(s) {unknown} (disponibles : {', '.join(AVAILABLE_LAYERS)})"
        )

    bbox = visible_bounds_wgs84(layout, projector, bleed=bleed)
    tier = tier or choose_tier(bbox)

    # Découpe sur le même rectangle que l'emprise chargée.
    left, top, right, bottom = frame_rect(layout, bleed=bleed)
    corners = np.asarray(
        [[left, top], [right, top], [right, bottom], [left, bottom]]
    )
    clip_pts = layout.invert(corners)
    clip_box = shapely.box(
        float(clip_pts[:, 0].min()),
        float(clip_pts[:, 1].min()),
        float(clip_pts[:, 0].max()),
        float(clip_pts[:, 1].max()),
    )

    forward = Transformer.from_crs("EPSG:4326", projector.crs, always_xy=True)

    def reproject(coords: np.ndarray) -> np.ndarray:
        x, y = forward.transform(coords[:, 0], coords[:, 1])
        return np.column_stack([np.asarray(x, np.float64), np.asarray(y, np.float64)])

    region = _osm_region_for(osm_store, bbox, tier)

    out: dict[str, list[Geometry]] = {}
    labels: list[Label] = []
    osm_layers: list[str] = []

    def place(geoms, names, populations, *, closed: bool, label_kind: str | None) -> None:
        """Reprojette, découpe, puis range en chemins ou en labels."""
        if len(geoms) == 0:
            return
        geoms = shapely.transform(geoms, reproject)
        geoms = shapely.intersection(geoms, clip_box)
        if label_kind == "country":
            labels.extend(_country_labels(geoms, names, layout, clip_box))
        elif label_kind:
            labels.extend(_city_labels(geoms, names, populations, layout))
        else:
            collected.extend(_to_viewbox_paths(geoms, layout, closed))

    for layer in layers:
        collected: list[Geometry] = []
        use_osm = _osm_provides(osm_store, region, layer)
        if use_osm:
            osm_layers.append(layer)
            osm_layer = layer_by_name(layer)
            place(
                *_read_osm(osm_store, region, layer, bbox, tier),
                closed=osm_layer.closed,
                label_kind="city" if osm_layer.label_field else None,
            )

        for dataset in _datasets_to_read(layer, tier, store, use_osm=use_osm):
            place(
                *_read_dataset(store, dataset, bbox, tier),
                closed=dataset.closed,
                label_kind=dataset.label_kind if dataset.label_field else None,
            )
        if collected:
            out[layer] = collected

    return BasemapResult(
        layers=out,
        labels=labels,
        tier=tier,
        osm_region=region.slug if osm_layers else None,
        osm_layers=tuple(osm_layers),
    )


def _osm_region_for(
    osm_store: OsmStore | None,
    bbox: tuple[float, float, float, float],
    tier: Tier,
) -> OsmRegion | None:
    if osm_store is None or tier.scale not in OSM_SCALES:
        return None
    return osm_store.covering(bbox)


def _osm_provides(
    osm_store: OsmStore | None, region: OsmRegion | None, layer: str
) -> bool:
    """Vrai si un extrait OSM alimente cette couche."""
    if osm_store is None or region is None:
        return False
    if layer not in (OSM_REPLACES | OSM_ADDS_TO):
        return False
    return osm_store.has_layer(region.slug, layer)


def osm_region_for(
    osm_store: OsmStore | None,
    bbox: tuple[float, float, float, float],
    tier: Tier,
) -> OsmRegion | None:
    """Extrait OSM utilisable pour cette emprise, ou None."""
    return _osm_region_for(osm_store, bbox, tier)


def _datasets_to_read(
    layer: str, tier: Tier, store: Store, *, use_osm: bool
) -> list[Dataset]:
    """Jeux Natural Earth réellement lus pour une couche."""
    out = []
    for dataset in datasets_for(layer, tier.scale):
        if dataset.tiers is not None and tier.name not in dataset.tiers:
            continue
        if dataset.optional and not store.has(dataset):
            continue
        # OSM a pris le relais : seul l'océan reste dessous.
        if use_osm and not dataset.keep_under_osm:
            continue
        out.append(dataset)
    return out


def planned_datasets(
    layers: tuple[str, ...],
    *,
    tier: Tier,
    store: Store,
    osm_store: OsmStore | None = None,
    region: OsmRegion | None = None,
) -> list[Dataset]:
    """Jeux Natural Earth nécessaires au rendu, OSM pris en compte.

    Sert à vérifier le cache avant de dessiner. La liste est produite par
    le même prédicat que la boucle de rendu : sans cela, l'outil
    réclamerait `ne_10m_roads` pour une carte dont les routes viennent
    d'OSM.
    """
    return [
        dataset
        for layer in layers
        for dataset in _datasets_to_read(
            layer, tier, store, use_osm=_osm_provides(osm_store, region, layer)
        )
    ]


def _city_labels(
    geoms: np.ndarray,
    names: list[str],
    populations: np.ndarray | None,
    layout: Layout,
) -> list[Label]:
    """Labels de villes, triés par population décroissante.

    Le tri est décroissant pour que la troncature éventuelle en aval
    garde les villes les plus importantes, et il est stabilisé par le nom
    afin que deux villes de même population sortent toujours dans le même
    ordre.
    """
    rows: list[Label] = []
    weights: list[float] = []
    for index, geom in enumerate(geoms):
        if geom is None or geom.is_empty or index >= len(names):
            continue
        coords = shapely.get_coordinates(geom)
        if len(coords) == 0 or not names[index]:
            continue
        placed = layout.apply(coords[:1])
        rows.append(Label(float(placed[0, 0]), float(placed[0, 1]), names[index]))
        weights.append(float(populations[index]) if populations is not None else 0.0)

    order = sorted(range(len(rows)), key=lambda i: (-weights[i], rows[i].text))
    return [rows[i] for i in order]


def _country_labels(
    geoms: np.ndarray,
    names: list[str],
    layout: Layout,
    clip_box,
) -> list[Label]:
    """Noms de pays, posés au centre de leur part visible.

    Le centroïde du polygone entier tomberait souvent hors du cadre — le
    centre de la France est loin d'une trace alpine. On travaille donc sur
    l'intersection avec le cadre, déjà calculée en amont.

    Le tri est par surface visible décroissante, et stabilisé par le nom :
    le pays qui domine la carte sort en premier.
    """
    rows: list[Label] = []
    areas: list[float] = []
    frame_area = float(shapely.area(clip_box)) or 1.0

    for index, geom in enumerate(geoms):
        if geom is None or geom.is_empty or index >= len(names):
            continue
        area = float(shapely.area(geom))
        if area / frame_area < _MIN_COUNTRY_SHARE or not names[index]:
            continue
        point = shapely.centroid(geom)
        # Un pays découpé en deux lobes par le cadre peut avoir son
        # centroïde hors des terres : on retombe sur un point garanti
        # intérieur.
        if not shapely.contains(geom, point):
            point = shapely.point_on_surface(geom)
        placed = layout.apply(shapely.get_coordinates(point))
        rows.append(
            Label(float(placed[0, 0]), float(placed[0, 1]), names[index], kind=COUNTRY)
        )
        areas.append(area)

    order = sorted(range(len(rows)), key=lambda i: (-areas[i], rows[i].text))
    return [rows[i] for i in order]


def suggest_cities(
    bbox: tuple[float, float, float, float],
    tier: Tier,
    *,
    store: Store,
    osm_store: OsmStore | None = None,
    limit: int = 8,
) -> list[tuple[str, float, float]]:
    """Plus grosses villes de l'emprise, sans filtre de rang/population.

    Contrairement aux labels automatiques (filtrés par palier de zoom
    pour ne pas surcharger la carte), cette fonction lit tout ce que les
    données locales connaissent dans le cadre — pour proposer à
    l'utilisateur des villes qu'un rendu automatique aurait pu écarter.
    Coordonnées WGS84 en sortie : c'est la même forme qu'un résultat
    Nominatim, réutilisable telle quelle par `web/geocode.py`.
    """
    candidates: dict[str, tuple[float, float, float]] = {}

    def collect(geoms, names, populations) -> None:
        for index, geom in enumerate(geoms):
            if geom is None or geom.is_empty or index >= len(names) or not names[index]:
                continue
            coords = shapely.get_coordinates(geom)
            if len(coords) == 0:
                continue
            lon, lat = float(coords[0, 0]), float(coords[0, 1])
            pop = float(populations[index]) if populations is not None else 0.0
            name = names[index]
            if name not in candidates or candidates[name][2] < pop:
                candidates[name] = (lon, lat, pop)

    for dataset in datasets_for("labels", tier.scale):
        if not dataset.label_field:
            continue
        path = store.layer_path(dataset)
        if not path.is_file():
            continue
        collect(
            *_read_features(
                path,
                bbox=bbox,
                max_rank=10**6,
                min_population=0,
                rank_field=dataset.rank_field,
                label_field=dataset.label_field,
                label_fallback_field=dataset.label_fallback_field,
                population_field=dataset.population_field,
            )
        )

    region = _osm_region_for(osm_store, bbox, tier)
    if region is not None and osm_store is not None and osm_store.has_layer(region.slug, "labels"):
        layer = layer_by_name("labels")
        collect(
            *_read_features(
                osm_store.layer_path(region.slug, "labels"),
                bbox=bbox,
                max_rank=10**6,
                min_population=0,
                rank_field=layer.rank_field,
                label_field=layer.label_field,
                population_field=layer.population_field,
            )
        )

    ranked = sorted(candidates.items(), key=lambda kv: (-kv[1][2], kv[0]))
    return [(name, lon, lat) for name, (lon, lat, _pop) in ranked[:limit]]
