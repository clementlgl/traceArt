"""Mise en page : bounding box projetée → repère viewBox SVG.

Le cadre suit le ratio de la trace (aucun format papier imposé), avec une
marge et un éventuel bandeau d'annotations en pied. Le grand côté du
viewBox vaut toujours `long_edge`, ce qui rend toutes les épaisseurs de
trait et tailles de texte des thèmes indépendantes de l'étendue
géographique de la trace.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from traceart.core.model import Track

# Étendue plancher (m) : évite une division par zéro sur une trace
# dégénérée (points confondus, trace parfaitement rectiligne).
_MIN_EXTENT = 1e-6
# Garde-fou contre les cas dégénérés seulement (trace quasi rectiligne) :
# le cadre épouse la trace, donc un ruban est un résultat légitime. Un
# plafond serré fabriquerait au contraire du vide sur le petit côté.
_MAX_ASPECT = 5.0


@dataclass(frozen=True, slots=True)
class Margins:
    top: float
    right: float
    bottom: float
    left: float

    @classmethod
    def uniform(cls, value: float) -> Margins:
        return cls(value, value, value, value)

    def with_bottom(self, value: float) -> Margins:
        return Margins(self.top, self.right, value, self.left)


@dataclass(frozen=True, slots=True)
class Layout:
    """Cadre de sortie et transformation affine vers ce cadre."""

    width: float
    height: float
    margins: Margins
    scale: float
    offset_x: float
    offset_y: float
    data_bounds: tuple[float, float, float, float]

    @property
    def content_width(self) -> float:
        return self.width - self.margins.left - self.margins.right

    @property
    def content_height(self) -> float:
        return self.height - self.margins.top - self.margins.bottom

    @property
    def long_edge(self) -> float:
        return max(self.width, self.height)

    def apply(self, coords: np.ndarray) -> np.ndarray:
        """Projeté → viewBox. L'axe y est inversé (SVG descend)."""
        x0, y0, _, _ = self.data_bounds
        x = (coords[:, 0] - x0) * self.scale + self.offset_x
        # offset_y est mesuré depuis le haut du cadre.
        y = self.height - ((coords[:, 1] - y0) * self.scale + self.offset_y)
        return np.column_stack([x, y])

    def invert(self, coords: np.ndarray) -> np.ndarray:
        """viewBox → projeté. Réciproque exacte de `apply`.

        Sert à retrouver l'emprise géographique du cadre : avec un ratio
        forcé ou un fond à fond perdu, le cadre découvre du terrain que la
        bounding box de la trace ne couvre pas.
        """
        x0, y0, _, _ = self.data_bounds
        x = (coords[:, 0] - self.offset_x) / self.scale + x0
        y = (self.height - coords[:, 1] - self.offset_y) / self.scale + y0
        return np.column_stack([x, y])


def combined_bounds(tracks: list[Track]) -> tuple[float, float, float, float]:
    """Bounding box englobant toutes les traces (mêmes unités)."""
    boxes = [t.bounds() for t in tracks if not t.is_empty]
    if not boxes:
        raise ValueError("aucune trace non vide")
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def fit_layout(
    bounds: tuple[float, float, float, float],
    *,
    long_edge: float = 1000.0,
    margin: float = 60.0,
    annotation_band: float = 0.0,
    aspect: float | None = None,
) -> Layout:
    """Construit le `Layout` pour une bounding box projetée.

    `aspect` (largeur/hauteur) force un ratio de cadre — utile pour un
    format papier. Laissé à None, le cadre épouse la trace.
    `annotation_band` réserve de la hauteur en pied de page pour le titre
    et les mesures.
    """
    x0, y0, x1, y1 = bounds
    data_w = max(x1 - x0, _MIN_EXTENT)
    data_h = max(y1 - y0, _MIN_EXTENT)

    margins = Margins.uniform(margin).with_bottom(margin + annotation_band)
    mh = margins.left + margins.right
    mv = margins.top + margins.bottom
    if long_edge - mh <= 0 or long_edge - mv <= 0:
        raise ValueError(
            f"marges ({margin}) et bandeau ({annotation_band}) trop grands "
            f"pour un grand côté de {long_edge}"
        )

    if aspect is None:
        # Le ratio est imposé à la zone de contenu, pas au cadre entier :
        # sinon le bandeau d'annotations écrase la carte en hauteur et
        # laisse deux bandes vides à gauche et à droite.
        ratio = data_w / data_h
        ratio = min(max(ratio, 1.0 / _MAX_ASPECT), _MAX_ASPECT)
        width = long_edge
        content_h = (width - mh) / ratio
        height = content_h + mv
        if height > long_edge:
            height = long_edge
            width = (height - mv) * ratio + mh
    elif aspect >= 1.0:
        width, height = long_edge, long_edge / aspect
    else:
        width, height = long_edge * aspect, long_edge

    avail_w = width - margins.left - margins.right
    avail_h = height - margins.top - margins.bottom
    if avail_w <= 0 or avail_h <= 0:
        raise ValueError(
            f"marges trop grandes ({margin}) pour un cadre {width:.0f}×{height:.0f}"
        )

    # Une seule échelle sur les deux axes : sinon la trace est déformée.
    scale = min(avail_w / data_w, avail_h / data_h)
    # Centrage du contenu dans la zone disponible.
    offset_x = margins.left + (avail_w - data_w * scale) / 2.0
    offset_y = margins.bottom + (avail_h - data_h * scale) / 2.0

    return Layout(
        width=width,
        height=height,
        margins=margins,
        scale=scale,
        offset_x=offset_x,
        offset_y=offset_y,
        data_bounds=(x0, y0, x1, y1),
    )


def layout_track(track: Track, layout: Layout) -> Track:
    """Applique la mise en page à une trace projetée."""
    return track.with_segments(
        [seg.with_coords(layout.apply(seg.coords)) for seg in track.segments],
        crs="viewbox",
    )
