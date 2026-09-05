"""Statistiques de trace : distance, dénivelé, durée, profil d'élévation.

À calculer sur la trace nettoyée mais AVANT simplification : Douglas-Peucker
raccourcit la distance mesurée et écrête le dénivelé.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
from pyproj import Geod

from traceart.core.model import Track

_GEOD = Geod(ellps="WGS84")

# Dénivelé : le lissage précède l'accumulation. L'hystérésis seule ne
# suffit pas — un bruit alterné de ±3 m a une amplitude de 6 m, donc
# franchit n'importe quelle bande morte réaliste et fabrique des centaines
# de mètres de D+ sur une longue trace.
_ELEVATION_SMOOTH_WINDOW = 7
_ELEVATION_DEADBAND_M = 3.0
# En dessous de cette vitesse on considère la trace à l'arrêt.
_MOVING_SPEED_MIN_KMH = 1.0


@dataclass(frozen=True, slots=True)
class TrackStats:
    """Mesures agrégées d'une trace, prêtes pour les annotations."""

    n_points: int
    n_segments: int
    distance_m: float
    ascent_m: float
    descent_m: float
    ele_min: float
    ele_max: float
    duration_s: float
    moving_s: float
    start_time: datetime | None
    end_time: datetime | None
    bounds: tuple[float, float, float, float]
    # Profil : distance cumulée (m) et élévation (m), mêmes longueurs.
    profile_dist: np.ndarray
    profile_ele: np.ndarray

    @property
    def distance_km(self) -> float:
        return self.distance_m / 1000.0

    @property
    def has_elevation(self) -> bool:
        return len(self.profile_ele) > 0 and bool(np.isfinite(self.profile_ele).any())

    @property
    def avg_speed_kmh(self) -> float:
        if self.moving_s <= 0:
            return 0.0
        return self.distance_m / self.moving_s * 3.6

    def format_duration(self, seconds: float | None = None) -> str:
        """Durée en `12 h 34` ou `34 min`. Chaîne vide si indisponible."""
        s = self.duration_s if seconds is None else seconds
        if not np.isfinite(s) or s <= 0:
            return ""
        total_min = round(s / 60)
        hours, minutes = divmod(total_min, 60)
        return f"{hours} h {minutes:02d}" if hours else f"{minutes} min"


def _smooth_elevation(values: np.ndarray, window: int) -> np.ndarray:
    """Moyenne glissante centrée, bords prolongés par la valeur extrême.

    Une moyenne — et non une médiane : le bruit altimétrique alterne d'un
    point au suivant, motif qu'un filtre médian de fenêtre impaire laisse
    passer intact. Les `window // 2` premiers et derniers points sont
    légèrement rabotés, ce qui est sans effet sur une trace réelle de
    plusieurs milliers de points.
    """
    if window <= 1 or len(values) < window:
        return values
    half = window // 2
    padded = np.pad(values, half, mode="edge")
    kernel = np.full(window, 1.0 / window)
    return np.convolve(padded, kernel, mode="valid")


def _accumulate_elevation(ele: np.ndarray) -> tuple[float, float]:
    """Dénivelés positif et négatif, après lissage puis hystérésis."""
    valid = ele[np.isfinite(ele)]
    if len(valid) < 2:
        return 0.0, 0.0
    valid = _smooth_elevation(valid, _ELEVATION_SMOOTH_WINDOW)

    ascent = descent = 0.0
    ref = float(valid[0])
    for value in valid[1:]:
        delta = float(value) - ref
        if delta > _ELEVATION_DEADBAND_M:
            ascent += delta
            ref = float(value)
        elif delta < -_ELEVATION_DEADBAND_M:
            descent += -delta
            ref = float(value)
    return ascent, descent


def compute_stats(track: Track) -> TrackStats:
    """Agrège les mesures d'une trace en WGS84."""
    if track.crs != "EPSG:4326":
        raise ValueError(f"compute_stats attend du WGS84, reçu {track.crs}")
    if track.is_empty:
        raise ValueError("trace vide : pas de statistiques")

    distance = 0.0
    ascent = descent = 0.0
    moving = 0.0
    times: list[np.ndarray] = []
    prof_dist: list[np.ndarray] = []
    prof_ele: list[np.ndarray] = []
    cumulative = 0.0

    for seg in track.segments:
        lons, lats = seg.coords[:, 0], seg.coords[:, 1]
        _, _, step = _GEOD.inv(lons[:-1], lats[:-1], lons[1:], lats[1:])
        step = np.abs(np.asarray(step, dtype=np.float64))
        distance += float(step.sum())

        seg_asc, seg_desc = _accumulate_elevation(seg.ele)
        ascent += seg_asc
        descent += seg_desc

        # Le profil est continu d'un segment au suivant : la distance
        # cumulée ne repart pas de zéro, sinon le graphe se replie.
        along = np.concatenate([[0.0], np.cumsum(step)]) + cumulative
        cumulative = float(along[-1])
        prof_dist.append(along)
        prof_ele.append(seg.ele)

        if seg.has_time:
            times.append(seg.time)
            dt = np.diff(seg.time)
            with np.errstate(invalid="ignore", divide="ignore"):
                speed = np.where(dt > 0, step / dt * 3.6, 0.0)
            moving += float(dt[np.nan_to_num(speed) >= _MOVING_SPEED_MIN_KMH].sum())

    all_ele = np.concatenate(prof_ele) if prof_ele else np.empty(0)
    finite_ele = all_ele[np.isfinite(all_ele)]

    start_time = end_time = None
    duration = 0.0
    if times:
        stacked = np.concatenate(times)
        finite_t = stacked[np.isfinite(stacked)]
        if len(finite_t) >= 2:
            t0, t1 = float(finite_t.min()), float(finite_t.max())
            duration = t1 - t0
            start_time = datetime.fromtimestamp(t0, tz=UTC)
            end_time = datetime.fromtimestamp(t1, tz=UTC)

    return TrackStats(
        n_points=track.n_points,
        n_segments=len(track.segments),
        distance_m=distance,
        ascent_m=ascent,
        descent_m=descent,
        ele_min=float(finite_ele.min()) if len(finite_ele) else float("nan"),
        ele_max=float(finite_ele.max()) if len(finite_ele) else float("nan"),
        duration_s=duration,
        moving_s=moving if moving > 0 else duration,
        start_time=start_time,
        end_time=end_time,
        bounds=track.bounds(),
        profile_dist=np.concatenate(prof_dist) if prof_dist else np.empty(0),
        profile_ele=all_ele,
    )
