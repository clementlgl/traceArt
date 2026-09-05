"""Projection WGS84 → plan.

Trois stratégies :

- `auto` (défaut) : Lambert conforme conique centrée sur la trace. Conforme
  = les angles sont préservés, donc la forme de la trace reste fidèle et
  reconnaissable, ce qui est le seul critère qui compte pour un poster.
- `webmercator` : EPSG:3857, utile pour comparer avec un fond de tuiles.
- `utm` : zone UTM déduite du centroïde, pour les traces locales.

Une valeur `EPSG:xxxx` explicite est également acceptée.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from pyproj import CRS, Transformer

from traceart.core.model import Track

# Au-delà de cette étendue, une conique centrée n'a plus de sens.
_LCC_MAX_SPAN_DEG = 60.0
# Parallèles standard placés au sixième et aux cinq sixièmes de l'étendue
# en latitude : répartition classique qui minimise la distorsion.
_LCC_PARALLEL_FRACTIONS = (1.0 / 6.0, 5.0 / 6.0)

MODES = ("auto", "webmercator", "utm")


@dataclass(frozen=True, slots=True)
class Projector:
    """Transformation lon/lat → x/y, avec sa définition CRS pour traçabilité."""

    crs: CRS
    definition: str
    mode: str


def unwrap_longitudes(lons: np.ndarray) -> np.ndarray:
    """Recolle les longitudes de part et d'autre de l'antiméridien.

    Une trace Fidji donne des longitudes ~179 et ~-179 : brutes, leur
    bounding box couvre la planète. On ramène les valeurs négatives dans
    la continuité (+360) quand c'est le cas.
    """
    if len(lons) == 0:
        return lons
    span_raw = float(lons.max() - lons.min())
    if span_raw <= 180.0:
        return lons
    shifted = np.where(lons < 0, lons + 360.0, lons)
    if float(shifted.max() - shifted.min()) < span_raw:
        return shifted
    return lons


def _all_lonlat(tracks: list[Track]) -> tuple[np.ndarray, np.ndarray]:
    parts = [s.coords for t in tracks for s in t.segments if len(s)]
    if not parts:
        raise ValueError("aucun point à projeter")
    allc = np.concatenate(parts)
    return unwrap_longitudes(allc[:, 0]), allc[:, 1]


def choose_projection(tracks: list[Track], mode: str = "auto") -> Projector:
    """Construit le `Projector` adapté à l'étendue des traces fournies."""
    if mode.upper().startswith("EPSG:") or mode.startswith("+proj="):
        return Projector(crs=CRS.from_user_input(mode), definition=mode, mode="explicit")
    if mode not in MODES:
        raise ValueError(f"mode de projection inconnu : {mode!r} (attendu {MODES} ou EPSG:xxxx)")

    lons, lats = _all_lonlat(tracks)
    lon0 = float((lons.min() + lons.max()) / 2.0)
    lat0 = float((lats.min() + lats.max()) / 2.0)

    if mode == "webmercator":
        return Projector(crs=CRS.from_epsg(3857), definition="EPSG:3857", mode="webmercator")

    if mode == "utm":
        zone = int((lon0 + 180.0) // 6.0) + 1
        epsg = (32600 if lat0 >= 0 else 32700) + zone
        return Projector(crs=CRS.from_epsg(epsg), definition=f"EPSG:{epsg}", mode="utm")

    span = max(float(lons.max() - lons.min()), float(lats.max() - lats.min()))
    if span > _LCC_MAX_SPAN_DEG:
        return Projector(crs=CRS.from_epsg(3857), definition="EPSG:3857", mode="webmercator")

    lo, hi = _LCC_PARALLEL_FRACTIONS
    lat_min, lat_max = float(lats.min()), float(lats.max())
    lat_1 = lat_min + lo * (lat_max - lat_min)
    lat_2 = lat_min + hi * (lat_max - lat_min)
    proj4 = (
        f"+proj=lcc +lat_0={lat0:.6f} +lon_0={lon0:.6f} "
        f"+lat_1={lat_1:.6f} +lat_2={lat_2:.6f} "
        "+x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
    )
    return Projector(crs=CRS.from_proj4(proj4), definition=proj4, mode="lcc")


def project_track(track: Track, projector: Projector) -> Track:
    """Reprojette une trace WGS84 vers le plan du `Projector`."""
    if track.crs != "EPSG:4326":
        raise ValueError(f"project_track attend du WGS84, reçu {track.crs}")

    transformer = Transformer.from_crs("EPSG:4326", projector.crs, always_xy=True)
    out = []
    for seg in track.segments:
        lon = unwrap_longitudes(seg.coords[:, 0])
        x, y = transformer.transform(lon, seg.coords[:, 1])
        coords = np.column_stack([np.asarray(x, np.float64), np.asarray(y, np.float64)])
        finite = np.isfinite(coords).all(axis=1)
        placed = seg.with_coords(coords)
        out.append(placed if finite.all() else placed.take(finite))
    return track.with_segments(out, crs=projector.definition)
