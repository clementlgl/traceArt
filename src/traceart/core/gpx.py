"""Parsing GPX en streaming.

Utilise `lxml.etree.iterparse` : les fichiers de traces réelles atteignent
facilement plusieurs Mo / dizaines de milliers de points, on ne charge donc
jamais l'arbre entier en mémoire.

Sont extraits les `<trkpt>` (groupés par `<trkseg>`, tous les `<trk>`
confondus) et les `<rtept>` (groupés par `<rte>`). Les `<wpt>` de premier
niveau sont ignorés : ce sont des POI, pas la trace.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

import numpy as np
from lxml import etree

from traceart.core.model import Segment, Track
from traceart.errors import UserError

# Conteneurs dont la fin ferme le segment courant.
_SEGMENT_TAGS = frozenset({"trkseg", "rte"})
# Points de trace retenus. `wpt` est volontairement absent.
_POINT_TAGS = frozenset({"trkpt", "rtept"})
# Éléments dont la fin déclenche un nettoyage mémoire.
_PRUNE_TAGS = _SEGMENT_TAGS | {"trk", "metadata", "wpt"}


class GpxError(UserError):
    """Fichier GPX illisible ou sans aucun point de trace."""


def _localname(tag: object) -> str:
    """Nom d'élément sans namespace. Les commentaires/PI renvoient ''."""
    if not isinstance(tag, str):
        return ""
    return tag.rpartition("}")[2]


def _parse_time(text: str | None) -> float:
    """Horodatage ISO 8601 → secondes epoch UTC. NaN si absent ou invalide."""
    if not text:
        return math.nan
    try:
        return datetime.fromisoformat(text.strip()).timestamp()
    except ValueError:
        return math.nan


def _parse_float(text: str | None) -> float:
    if not text:
        return math.nan
    try:
        return float(text.strip())
    except ValueError:
        return math.nan


def _read_point(el: etree._Element) -> tuple[float, float, float, float] | None:
    """(lon, lat, ele, time) pour un trkpt/rtept, ou None si coords invalides."""
    lon = _parse_float(el.get("lon"))
    lat = _parse_float(el.get("lat"))
    if not (math.isfinite(lon) and math.isfinite(lat)):
        return None
    if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
        return None

    ele = math.nan
    tstamp = math.nan
    for child in el:
        name = _localname(child.tag)
        if name == "ele":
            ele = _parse_float(child.text)
        elif name == "time":
            tstamp = _parse_time(child.text)
    return lon, lat, ele, tstamp


def _to_segment(rows: list[tuple[float, float, float, float]]) -> Segment:
    arr = np.asarray(rows, dtype=np.float64)
    return Segment(coords=arr[:, 0:2].copy(), ele=arr[:, 2].copy(), time=arr[:, 3].copy())


def _prune(el: etree._Element) -> None:
    """Libère l'élément et ses frères déjà traités."""
    el.clear()
    parent = el.getparent()
    if parent is None:
        return
    while len(parent) > 1:
        del parent[0]


def parse_gpx(path: str | Path) -> Track:
    """Lit un fichier GPX et renvoie une `Track` en WGS84 (lon, lat).

    Lève `GpxError` si le fichier n'est pas parsable ou ne contient aucun
    point de trace exploitable.
    """
    path = Path(path)
    segments: list[Segment] = []
    current: list[tuple[float, float, float, float]] = []
    name: str | None = None
    creator: str | None = None
    # Profondeur de <wpt> en cours : on ignore tout ce qu'il contient.
    in_waypoint = 0

    def close_segment() -> None:
        nonlocal current
        if len(current) >= 2:
            segments.append(_to_segment(current))
        current = []

    try:
        context = etree.iterparse(
            str(path),
            events=("start", "end"),
            recover=True,
            resolve_entities=False,
            no_network=True,
        )
        for event, el in context:
            tag = _localname(el.tag)

            if event == "start":
                if tag == "wpt":
                    in_waypoint += 1
                elif tag == "gpx" and creator is None:
                    creator = el.get("creator")
                continue

            if tag == "wpt":
                in_waypoint = max(0, in_waypoint - 1)
                _prune(el)
                continue
            if in_waypoint:
                continue

            if tag in _POINT_TAGS:
                point = _read_point(el)
                if point is not None:
                    current.append(point)
                el.clear()
            elif tag in _SEGMENT_TAGS:
                close_segment()
                _prune(el)
            elif tag == "name" and name is None:
                parent = el.getparent()
                if parent is not None and _localname(parent.tag) in {"metadata", "trk", "rte"}:
                    text = (el.text or "").strip()
                    name = text or None
            elif tag in _PRUNE_TAGS:
                _prune(el)
    except etree.XMLSyntaxError as exc:
        raise GpxError(f"{path.name} : XML illisible ({exc})") from exc
    except OSError as exc:
        raise GpxError(f"{path.name} : lecture impossible ({exc})") from exc

    # Un GPX peut clore ses points sans <trkseg> fermant exploitable.
    close_segment()

    if not segments:
        raise GpxError(f"{path.name} : aucun point de trace (<trkpt>/<rtept>) trouvé")

    meta = {"creator": creator} if creator else {}
    return Track(segments=segments, name=name, source=path, crs="EPSG:4326", meta=meta)


def parse_many(paths: list[str | Path], *, strict: bool = False) -> list[Track]:
    """Parse plusieurs GPX. Sans `strict`, les fichiers en échec sont sautés.

    Le tri par nom de fichier garantit un ordre de sortie déterministe,
    indépendant de l'ordre de globbing du shell.
    """
    tracks: list[Track] = []
    errors: list[str] = []
    for p in sorted(paths, key=str):
        try:
            tracks.append(parse_gpx(p))
        except GpxError as exc:
            if strict:
                raise
            errors.append(str(exc))
    if not tracks:
        detail = " ; ".join(errors) if errors else "aucun fichier fourni"
        raise GpxError(f"aucune trace exploitable : {detail}")
    return tracks


def collect_gpx(inputs: Iterable[str | Path]) -> list[Path]:
    """Développe les dossiers en fichiers `.gpx` (récursif, ordre stable).

    Partagé par le CLI (arguments de la ligne de commande) et par
    l'interface web (chemins reconstitués depuis un lot d'upload) : les
    deux doivent accepter indifféremment un fichier ou un dossier, et
    refuser un lot qui ne contient aucune trace exploitable.
    """
    found: list[Path] = []
    for raw in inputs:
        item = Path(raw)
        if item.is_dir():
            found.extend(sorted(item.rglob("*.gpx")))
        elif item.is_file():
            found.append(item)
        else:
            raise GpxError(f"chemin introuvable : {item}")
    # dict.fromkeys : dédoublonne sans perdre l'ordre.
    unique = list(dict.fromkeys(p.resolve() for p in found))
    if not unique:
        raise GpxError("aucun fichier .gpx trouvé dans les chemins donnés")
    return unique
