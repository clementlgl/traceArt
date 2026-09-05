"""Liste des villes ajoutées manuellement, par session.

Le fichier `_labels.json` vit **dans** le dossier de session
(`web/uploads.py:session_dir`), pas dans un emplacement séparé : il
hérite ainsi gratuitement de tout le cycle de vie déjà en place — balayé
par `sweep_expired_sessions`, et **effacé à chaque nouvel upload**
(`uploads.save()` fait `shutil.rmtree` sur tout le dossier de session
avant d'y réécrire les `.gpx`). C'est un choix délibéré, pas un effet de
bord subi : une nouvelle trace change le contexte géographique, repartir
à zéro sur les villes ajoutées est cohérent.
"""

from __future__ import annotations

import json
from pathlib import Path

from traceart.web.geocode import GeocodeResult
from traceart.web.uploads import session_dir

_FILENAME = "_labels.json"


def labels_path(work_dir: Path, session_id: str) -> Path:
    return session_dir(work_dir, session_id) / _FILENAME


def read_labels(work_dir: Path, session_id: str) -> list[GeocodeResult]:
    path = labels_path(work_dir, session_id)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # Un fichier corrompu ne doit pas faire échouer tout le rendu :
        # la pire conséquence acceptable est de perdre la liste.
        return []
    return [
        GeocodeResult(name=str(e["name"]), lat=float(e["lat"]), lon=float(e["lon"]))
        for e in raw
        if isinstance(e, dict) and {"name", "lat", "lon"} <= e.keys()
    ]


def _write_labels(work_dir: Path, session_id: str, entries: list[GeocodeResult]) -> None:
    path = labels_path(work_dir, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [{"name": e.name, "lat": e.lat, "lon": e.lon} for e in entries]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def add_label(work_dir: Path, session_id: str, result: GeocodeResult) -> list[GeocodeResult]:
    """Ajoute une ville, dédoublonnée par nom (le second ajout du même
    nom remplace le premier plutôt que de le dupliquer dans la liste)."""
    entries = [e for e in read_labels(work_dir, session_id) if e.name != result.name]
    entries.append(result)
    _write_labels(work_dir, session_id, entries)
    return entries


def remove_label(work_dir: Path, session_id: str, name: str) -> list[GeocodeResult]:
    entries = [e for e in read_labels(work_dir, session_id) if e.name != name]
    _write_labels(work_dir, session_id, entries)
    return entries
