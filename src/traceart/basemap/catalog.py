"""Catalogue des jeux de données Natural Earth utilisés comme fond.

Natural Earth est du domaine public, servi en zip de shapefiles, et se
décline en trois résolutions (10m, 50m, 110m). Le choix de la résolution
et le filtrage par importance dépendent de l'étendue de la trace : à
l'échelle d'un pays on ne veut ni les routes secondaires ni les 7 000
rivières du monde.

Chaque couche du thème (`water`, `coastline`, …) est alimentée par un ou
plusieurs jeux Natural Earth.
"""

from __future__ import annotations

from dataclasses import dataclass

from traceart.layers import AVAILABLE_LAYERS, DEFAULT_LAYERS

# Ré-exportés : les appelants du catalogue n'ont pas à connaître le
# registre, mais il n'existe qu'une définition.
__all__ = ["AVAILABLE_LAYERS", "CATALOG", "DEFAULT_LAYERS", "Dataset", "datasets_for"]

BASE_URL = "https://naturalearth.s3.amazonaws.com"

SCALES = ("10m", "50m", "110m")


@dataclass(frozen=True, slots=True)
class Dataset:
    """Un jeu Natural Earth, rattaché à une couche de thème."""

    layer: str
    name: str
    category: str  # "physical" | "cultural"
    scale: str
    closed: bool  # polygone à remplir, ou ligne / point
    rank_field: str | None = None
    label_field: str | None = None
    # Champ de repli quand `label_field` est vide pour une entité : les
    # traductions de Natural Earth ne couvrent pas tous les territoires.
    label_fallback_field: str | None = None
    population_field: str | None = None
    # "city" : le label se pose sur le point. "country" : sur le
    # centroïde de la part visible du polygone, avec son propre style.
    label_kind: str = "city"
    # Paliers de zoom où le jeu a du sens. None = tous. Les limites
    # administratives internes n'ont aucun champ d'importance : à
    # l'échelle d'un pays, dessiner ses 96 départements sature le fond.
    tiers: tuple[str, ...] | None = None
    # Supplément : son absence du cache n'empêche pas le rendu, elle
    # l'appauvrit seulement. Un cache rempli avant l'ajout d'un
    # supplément reste donc utilisable tel quel.
    optional: bool = False
    # Reste dessiné même quand un extrait OSM prend le relais sur sa
    # couche. Vrai pour l'océan : OSM ne fournit aucun polygone océan.
    keep_under_osm: bool = False

    @property
    def url(self) -> str:
        return f"{BASE_URL}/{self.scale}_{self.category}/{self.name}.zip"

    @property
    def key(self) -> str:
        """Identifiant stable, utilisé comme nom de fichier en cache."""
        return self.name


def _physical(layer: str, stem: str, scale: str, *, closed: bool, rank: str | None) -> Dataset:
    return Dataset(
        layer=layer,
        name=f"ne_{scale}_{stem}",
        category="physical",
        scale=scale,
        closed=closed,
        rank_field=rank,
    )


def _cultural(layer: str, stem: str, scale: str, *, closed: bool, rank: str | None) -> Dataset:
    return Dataset(
        layer=layer,
        name=f"ne_{scale}_{stem}",
        category="cultural",
        scale=scale,
        closed=closed,
        rank_field=rank,
    )


def _catalog() -> tuple[Dataset, ...]:
    items: list[Dataset] = []
    for scale in SCALES:
        items.append(
            Dataset(
                layer="water",
                name=f"ne_{scale}_ocean",
                category="physical",
                scale=scale,
                closed=True,
                keep_under_osm=True,
            )
        )
        # Pas de filtrage par `scalerank` sur les lacs : un lac est une
        # surface, son étendue à l'écran suffit à décider s'il est
        # visible. Le rang va d'ailleurs jusqu'à 9 dans ne_10m_lakes,
        # au-delà du plafond du palier « région » — 35 % des lacs
        # disparaissaient sans raison.
        items.append(_physical("water", "lakes", scale, closed=True, rank=None))
        items.append(_physical("coastline", "coastline", scale, closed=False, rank=None))
        items.append(
            _physical(
                "rivers", "rivers_lake_centerlines", scale, closed=False, rank="scalerank"
            )
        )
        items.append(
            _cultural(
                "borders",
                "admin_0_boundary_lines_land",
                scale,
                closed=False,
                rank=None,
            )
        )
        items.append(
            Dataset(
                layer="countries",
                name=f"ne_{scale}_admin_0_countries",
                category="cultural",
                scale=scale,
                closed=True,
                # NAME_FR : Natural Earth publie les noms de pays
                # traduits. Le repli sur NAME est géré à la lecture.
                label_field="NAME_FR",
                label_fallback_field="NAME",
                label_kind="country",
            )
        )
        items.append(
            Dataset(
                layer="labels",
                name=f"ne_{scale}_populated_places",
                category="cultural",
                scale=scale,
                closed=False,
                rank_field="SCALERANK",
                label_field="NAME",
                population_field="POP_MAX",
            )
        )
    # Limites administratives internes : absentes du 110m chez Natural Earth.
    items.extend(
        Dataset(
            layer="boundaries",
            name=f"ne_{scale}_admin_1_states_provinces_lines",
            category="cultural",
            scale=scale,
            closed=False,
            tiers=("local",),
        )
        for scale in ("10m", "50m")
    )
    # Natural Earth sort les lacs régionaux dans des jeux séparés : le
    # `ne_10m_lakes` mondial ne contient aucun lac du centre-sud de la
    # France. Ces suppléments n'existent qu'en 10m. `lakes_pluvial` et
    # `lakes_historic` sont volontairement absents : ce sont des
    # paléo-lacs, ils n'existent plus.
    items.extend(
        Dataset(
            layer="water",
            name=f"ne_10m_{stem}",
            category="physical",
            scale="10m",
            closed=True,
            optional=True,
        )
        for stem in ("lakes_europe", "lakes_north_america")
    )

    # Les routes n'existent qu'en 10m : c'est la seule résolution où elles
    # ont du sens. Aux échelles plus larges, la couche est simplement vide.
    items.append(_cultural("roads", "roads", "10m", closed=False, rank="scalerank"))
    return tuple(items)


CATALOG: tuple[Dataset, ...] = _catalog()



def datasets_for(layer: str, scale: str) -> tuple[Dataset, ...]:
    """Jeux alimentant une couche à une résolution donnée.

    Si la couche n'existe pas à cette résolution (routes en 50m, limites
    internes en 110m), on retombe sur la résolution la plus proche
    disponible plutôt que de rendre une couche vide.
    """
    exact = tuple(d for d in CATALOG if d.layer == layer and d.scale == scale)
    if exact:
        return exact
    for fallback in SCALES:
        candidates = tuple(d for d in CATALOG if d.layer == layer and d.scale == fallback)
        if candidates:
            return candidates
    return ()


def dataset_by_name(name: str) -> Dataset | None:
    return next((d for d in CATALOG if d.name == name), None)
