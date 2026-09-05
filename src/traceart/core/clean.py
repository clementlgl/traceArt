"""Nettoyage des traces : doublons, points aberrants, pauses.

Travaille en WGS84 : les distances sont géodésiques (`pyproj.Geod`), donc
justes quelle que soit la latitude — contrairement à un calcul fait après
projection Web Mercator.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from pyproj import Geod

from traceart.core.model import Segment, Track

_GEOD = Geod(ellps="WGS84")

# Un degré de latitude en mètres (WGS84, valeur moyenne).
_M_PER_DEG_LAT = 111_132.0

# Sous ce nombre de points, un segment n'est plus une trace.
_MIN_SEGMENT_POINTS = 2


@dataclass(frozen=True, slots=True)
class CleanConfig:
    """Seuils de nettoyage.

    max_speed_kmh: vitesse au-delà de laquelle un saut est jugé impossible.
        300 km/h laisse passer moto et train sans laisser passer un
        téléport GPS. Utilisé seulement si le GPX porte des timestamps.
    max_jump_m: saut de position considéré aberrant en l'absence de temps,
        combiné à `jump_factor` × l'espacement médian du segment.
    jump_factor: multiplicateur de l'espacement courant (1er quartile)
        pour la détection
        d'aberration relative (robuste aux traces peu ou très denses).
    pause_radius_m: rayon en dessous duquel des points consécutifs sont
        considérés immobiles et fusionnés en un seul.
    dedupe_m: distance en dessous de laquelle deux points consécutifs sont
        des doublons.
    """

    max_speed_kmh: float = 300.0
    max_jump_m: float = 1000.0
    jump_factor: float = 25.0
    pause_radius_m: float = 12.0
    dedupe_m: float = 0.5

    enable_dedupe: bool = True
    enable_outliers: bool = True
    enable_pauses: bool = True


@dataclass(frozen=True, slots=True)
class CleanReport:
    """Ce que le nettoyage a retiré, pour affichage CLI et tests."""

    points_in: int
    points_out: int
    duplicates: int
    outliers: int
    paused: int
    segments_dropped: int

    @property
    def removed(self) -> int:
        return self.points_in - self.points_out


def _step_distances(coords: np.ndarray) -> np.ndarray:
    """Distances géodésiques (m) entre points consécutifs. Longueur N-1."""
    if len(coords) < 2:
        return np.empty(0, dtype=np.float64)
    lons, lats = coords[:, 0], coords[:, 1]
    _, _, dist = _GEOD.inv(lons[:-1], lats[:-1], lons[1:], lats[1:])
    return np.abs(np.asarray(dist, dtype=np.float64))


def _drop_duplicates(seg: Segment, cfg: CleanConfig) -> tuple[Segment, int]:
    dist = _step_distances(seg.coords)
    keep = np.ones(len(seg), dtype=bool)
    keep[1:] = dist > cfg.dedupe_m
    removed = int((~keep).sum())
    return (seg.take(keep), removed) if removed else (seg, 0)


def _drop_outliers(seg: Segment, cfg: CleanConfig) -> tuple[Segment, int]:
    """Retire les points isolés par un saut de position irréaliste.

    Un point n'est retiré que si le saut qui y mène ET le saut qui en part
    sont tous deux excessifs : c'est la signature d'un pic GPS isolé. Un
    seul saut excessif suivi d'une suite cohérente est un vrai déplacement
    (reprise d'enregistrement, trou de couverture) et doit être conservé.
    """
    n = len(seg)
    if n < 3:
        return seg, 0

    dist = _step_distances(seg.coords)
    # Premier quartile plutôt que médiane : sur un segment court et très
    # bruité, la moitié des pas peut être aberrante et contaminer la
    # médiane, ce qui ferait exploser le seuil et désarmerait la détection.
    positive = dist[dist > 0]
    typical_step = float(np.percentile(positive, 25)) if len(positive) else 0.0
    threshold = max(cfg.max_jump_m, cfg.jump_factor * typical_step)

    if seg.has_time:
        dt = np.diff(seg.time)
        with np.errstate(invalid="ignore", divide="ignore"):
            speed_kmh = np.where(dt > 0, dist / dt * 3.6, np.nan)
        too_fast = np.nan_to_num(speed_kmh, nan=0.0) > cfg.max_speed_kmh
        bad_step = too_fast | (dist > threshold)
    else:
        bad_step = dist > threshold

    # Point i (intérieur) suspect si bad_step[i-1] et bad_step[i].
    keep = np.ones(n, dtype=bool)
    keep[1:-1] = ~(bad_step[:-1] & bad_step[1:])
    removed = int((~keep).sum())
    return (seg.take(keep), removed) if removed else (seg, 0)


def _drop_pauses(seg: Segment, cfg: CleanConfig) -> tuple[Segment, int]:
    """Fusionne les grappes de points immobiles en un point unique.

    On avance en gardant un point d'ancrage : tout point à moins de
    `pause_radius_m` de l'ancre est écarté. Cela supprime les feux rouges
    et les arrêts casse-croûte sans raboter les virages serrés, qui
    s'éloignent de l'ancre bien plus vite que le rayon.
    """
    n = len(seg)
    if n < 3:
        return seg, 0

    # Métrique locale plane : à l'échelle de quelques mètres l'écart avec
    # une distance géodésique est sous le millimètre, pour un coût nul.
    lat0 = float(np.median(seg.coords[:, 1]))
    mx = _M_PER_DEG_LAT * np.cos(np.radians(lat0))
    px = seg.coords[:, 0] * mx
    py = seg.coords[:, 1] * _M_PER_DEG_LAT
    r2 = cfg.pause_radius_m**2

    keep = np.zeros(n, dtype=bool)
    keep[0] = True
    ax, ay = px[0], py[0]
    for i in range(1, n):
        dx = px[i] - ax
        dy = py[i] - ay
        if dx * dx + dy * dy > r2:
            keep[i] = True
            ax, ay = px[i], py[i]
    # Le dernier point est l'arrivée : il porte du sens, on le garde.
    keep[-1] = True
    removed = int((~keep).sum())
    return (seg.take(keep), removed) if removed else (seg, 0)


def clean_track(track: Track, cfg: CleanConfig | None = None) -> tuple[Track, CleanReport]:
    """Nettoie chaque segment et renvoie la trace nettoyée + un rapport."""
    if track.crs != "EPSG:4326":
        raise ValueError(f"clean_track attend du WGS84, reçu {track.crs}")
    cfg = cfg or CleanConfig()

    out: list[Segment] = []
    n_dup = n_out = n_pause = 0
    points_in = track.n_points

    for seg in track.segments:
        cur = seg
        if cfg.enable_dedupe:
            cur, r = _drop_duplicates(cur, cfg)
            n_dup += r
        if cfg.enable_outliers:
            cur, r = _drop_outliers(cur, cfg)
            n_out += r
        if cfg.enable_pauses:
            cur, r = _drop_pauses(cur, cfg)
            n_pause += r
        if len(cur) >= _MIN_SEGMENT_POINTS:
            out.append(cur)

    report = CleanReport(
        points_in=points_in,
        points_out=sum(len(s) for s in out),
        duplicates=n_dup,
        outliers=n_out,
        paused=n_pause,
        segments_dropped=len(track.segments) - len(out),
    )
    return track.with_segments(out), report
