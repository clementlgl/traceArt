"""Registre des couches de fond — source unique de vérité.

Le nom d'une couche, son ordre de dessin, son libellé Inkscape et son
appartenance aux défauts vivaient dans trois modules différents
(`basemap/catalog.py`, `render/theme.py`, les thèmes TOML). La dérive
était inévitable, et elle a eu lieu : une couche `relief` figurait dans
l'ordre de dessin et dans les quatre thèmes sans qu'aucun jeu ne
l'alimente, tandis que `countries` et `labels` manquaient à l'ordre.

Ce module ne dépend de rien, donc `basemap` et `render` peuvent
l'importer tous les deux sans cycle.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LayerSpec:
    """Une couche de fond, du nom CLI au calque SVG."""

    name: str
    # Libellé lisible dans le panneau Inkscape.
    label: str
    # Dessinée par défaut quand `--layers` n'est pas donné.
    default: bool
    # Les couches de texte ne produisent aucun chemin : elles sont
    # rendues après la trace, pas dans l'ordre du fond.
    text_only: bool = False


# L'ordre de la séquence est l'ordre de dessin, du fond vers l'avant.
LAYERS: tuple[LayerSpec, ...] = (
    # Posés en premier : un aplat de fond, jamais par-dessus l'eau ou les
    # traits qui structurent la carte. Aucun jeu Natural Earth ne les
    # fournit (pas de couche mondiale équivalente) — uniquement des
    # extraits OSM (`boundary=national_park`/`protected_area`,
    # `leisure=nature_reserve`), donc invisibles hors palier 10m avec
    # extrait importé.
    LayerSpec("parks", "parcs naturels", default=False),
    LayerSpec("water", "plans d'eau", default=True),
    LayerSpec("coastline", "côtes", default=True),
    LayerSpec("rivers", "cours d'eau", default=True),
    LayerSpec("roads", "routes", default=False),
    # La limite interne passe sous la frontière : là où elles se
    # superposent, c'est la frontière qui doit se lire.
    LayerSpec("boundaries", "limites administratives", default=True),
    LayerSpec("borders", "frontières", default=True),
    LayerSpec("countries", "noms de pays", default=False, text_only=True),
    LayerSpec("labels", "villes", default=False, text_only=True),
)

AVAILABLE_LAYERS: tuple[str, ...] = tuple(spec.name for spec in LAYERS)
DEFAULT_LAYERS: tuple[str, ...] = tuple(spec.name for spec in LAYERS if spec.default)
# Couches porteuses de géométrie, dans l'ordre de dessin.
LAYER_ORDER: tuple[str, ...] = tuple(spec.name for spec in LAYERS if not spec.text_only)
LAYER_LABELS: dict[str, str] = {spec.name: spec.label for spec in LAYERS}
