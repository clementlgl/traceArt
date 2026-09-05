"""Choix de résolution et seuils d'importance selon l'étendue de la vue.

C'est ce qui rend le fond « abstrait » plutôt que documentaire : à
l'échelle d'un continent on ne garde que les fleuves majeurs et les
capitales ; à l'échelle d'une vallée, tout le réseau hydrographique et
les villages.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Tier:
    """Palier de zoom : résolution des données et seuils de filtrage."""

    name: str
    min_span_deg: float
    scale: str
    max_rank: int
    min_population: int
    # Seuil propre aux extraits OSM. Un extrait régional est deux ordres
    # de grandeur plus dense que Natural Earth : appliquer le même
    # plafond donnerait chaque ruisseau, chaque route communale et chaque
    # limite de commune — une carte topographique, pas un poster.
    osm_max_rank: int = 5
    # Population minimale, côté OSM. Distincte de `min_population` parce
    # que les deux sources ne comptent pas la même chose : `POP_MAX` de
    # Natural Earth est une population d'agglomération, le tag OSM
    # `population` celle de la commune. Bergame vaut 500 000 chez l'un et
    # 120 000 chez l'autre — au seuil de Natural Earth, l'arc alpin
    # n'aurait qu'un seul label.
    osm_min_population: int = 25_000


# Du plus large au plus serré. Le premier palier dont `min_span_deg` est
# atteint gagne.
TIERS: tuple[Tier, ...] = (
    Tier(
        "monde", 40.0, "110m",
        max_rank=3, min_population=3_000_000,
        osm_max_rank=1, osm_min_population=500_000,
    ),
    Tier(
        "continent", 12.0, "50m",
        max_rank=5, min_population=800_000,
        osm_max_rank=2, osm_min_population=150_000,
    ),
    Tier(
        "région", 2.5, "10m",
        max_rank=8, min_population=150_000,
        osm_max_rank=5, osm_min_population=25_000,
    ),
    # Palier local en OSM : fleuves et canaux, réseau primaire et
    # secondaire, limites de département. Ni ruisseau, ni route
    # tertiaire, ni limite de commune.
    Tier(
        "local", 0.0, "10m",
        max_rank=12, min_population=15_000,
        osm_max_rank=8, osm_min_population=3_000,
    ),
)


def choose_tier(bounds: tuple[float, float, float, float]) -> Tier:
    """Palier adapté à une bounding box WGS84 (min_lon, min_lat, max_lon, max_lat)."""
    min_lon, min_lat, max_lon, max_lat = bounds
    span = max(max_lon - min_lon, max_lat - min_lat)
    for tier in TIERS:
        if span >= tier.min_span_deg:
            return tier
    return TIERS[-1]
