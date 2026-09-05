"""Modèle de données : une trace = une liste de segments de coordonnées."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np


@dataclass(frozen=True, slots=True)
class Segment:
    """Portion continue d'une trace.

    `coords` est un tableau (N, 2). En sortie de parsing il contient
    (lon, lat) en degrés ; après projection, (x, y) dans l'unité du CRS
    cible ; après mise en page, (x, y) en unités de viewBox SVG.

    `ele` (mètres) et `time` (secondes epoch) sont de longueur N et
    contiennent NaN quand la donnée est absente du GPX.
    """

    coords: np.ndarray
    ele: np.ndarray
    time: np.ndarray

    def __post_init__(self) -> None:
        n = len(self.coords)
        if self.coords.ndim != 2 or self.coords.shape[1] != 2:
            raise ValueError(f"coords doit être de forme (N, 2), reçu {self.coords.shape}")
        if len(self.ele) != n or len(self.time) != n:
            raise ValueError("ele et time doivent avoir la même longueur que coords")

    def __len__(self) -> int:
        return len(self.coords)

    @property
    def x(self) -> np.ndarray:
        return self.coords[:, 0]

    @property
    def y(self) -> np.ndarray:
        return self.coords[:, 1]

    @property
    def has_time(self) -> bool:
        return bool(np.isfinite(self.time).any())

    @property
    def has_ele(self) -> bool:
        return bool(np.isfinite(self.ele).any())

    def take(self, mask_or_idx: np.ndarray) -> Segment:
        """Nouveau segment restreint aux indices (booléens ou entiers) donnés."""
        return Segment(
            coords=self.coords[mask_or_idx],
            ele=self.ele[mask_or_idx],
            time=self.time[mask_or_idx],
        )

    def with_coords(self, coords: np.ndarray) -> Segment:
        """Même segment avec de nouvelles coordonnées (même cardinalité)."""
        return replace(self, coords=coords)



@dataclass(frozen=True, slots=True)
class Track:
    """Une trace complète, issue d'un fichier GPX."""

    segments: list[Segment]
    name: str | None = None
    source: Path | None = None
    crs: str = "EPSG:4326"
    meta: dict[str, str] = field(default_factory=dict)

    @property
    def n_points(self) -> int:
        return sum(len(s) for s in self.segments)

    @property
    def is_empty(self) -> bool:
        return self.n_points == 0

    def bounds(self) -> tuple[float, float, float, float]:
        """(min_x, min_y, max_x, max_y) sur tous les segments non vides."""
        parts = [s.coords for s in self.segments if len(s)]
        if not parts:
            raise ValueError("trace vide : pas de bounding box")
        allc = np.concatenate(parts)
        return (
            float(allc[:, 0].min()),
            float(allc[:, 1].min()),
            float(allc[:, 0].max()),
            float(allc[:, 1].max()),
        )

    def with_segments(self, segments: list[Segment], crs: str | None = None) -> Track:
        kept = [s for s in segments if len(s) >= 2]
        return replace(self, segments=kept, crs=crs or self.crs)

    @property
    def label(self) -> str:
        if self.name:
            return self.name
        if self.source:
            return self.source.stem
        return "trace"
