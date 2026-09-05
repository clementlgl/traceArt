"""Simplification Douglas-Peucker.

Implémentation maison plutôt que `shapely.simplify` pour une raison
précise : on a besoin des *indices* conservés, pas seulement des
coordonnées. Cela permet de garder élévation et horodatage alignés sur les
points survivants — indispensable pour le profil d'élévation en encart.

La tolérance est exprimée dans l'unité des coordonnées du segment. En
pratique on simplifie après mise en page, donc en unités de viewBox : une
tolérance de 0,25 sur un grand côté de 1000 est invisible à l'œil tout en
divisant le nombre de points par 20 à 100.
"""

from __future__ import annotations

import numpy as np

from traceart.core.model import Segment, Track


def douglas_peucker_mask(points: np.ndarray, tolerance: float) -> np.ndarray:
    """Masque booléen des points conservés par Douglas-Peucker.

    Itératif (pile explicite) : une trace de 100 000 points ferait
    exploser la pile Python en récursif.
    """
    n = len(points)
    keep = np.zeros(n, dtype=bool)
    if n == 0:
        return keep
    keep[0] = True
    keep[-1] = True
    if n <= 2 or tolerance <= 0:
        keep[:] = True
        return keep

    stack: list[tuple[int, int]] = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        a = points[i]
        b = points[j]
        inner = points[i + 1 : j]
        ab = b - a
        len2 = float(ab @ ab)
        if len2 == 0.0:
            # Extrémités confondues : distance au point, pas à la droite.
            dist = np.hypot(inner[:, 0] - a[0], inner[:, 1] - a[1])
        else:
            # Produit vectoriel 2D explicite : np.cross sur des vecteurs
            # à 2 composantes est déprécié depuis NumPy 2.0.
            cross = ab[0] * (inner[:, 1] - a[1]) - ab[1] * (inner[:, 0] - a[0])
            dist = np.abs(cross) / np.sqrt(len2)
        k = int(np.argmax(dist))
        if float(dist[k]) > tolerance:
            pivot = i + 1 + k
            keep[pivot] = True
            stack.append((i, pivot))
            stack.append((pivot, j))
    return keep


def simplify_segment(seg: Segment, tolerance: float) -> Segment:
    if len(seg) <= 2 or tolerance <= 0:
        return seg
    return seg.take(douglas_peucker_mask(seg.coords, tolerance))


def simplify_track(track: Track, tolerance: float) -> Track:
    """Simplifie tous les segments d'une trace à tolérance constante."""
    if tolerance <= 0:
        return track
    return track.with_segments([simplify_segment(s, tolerance) for s in track.segments])
