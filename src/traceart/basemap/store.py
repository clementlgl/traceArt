"""Cache local des données de fond.

Le téléchargement est explicite (`traceart data fetch`) : l'outil ne va
jamais chercher 50 Mo sur le réseau au milieu d'un rendu. Une fois le
cache rempli, tout fonctionne hors ligne.

Les zips Natural Earth sont convertis en FlatGeobuf, qui porte un index
spatial : une requête par bounding box ne lit alors que les entités
concernées, au lieu de balayer les 57 000 tronçons de `ne_10m_roads`.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from traceart.basemap.catalog import CATALOG, Dataset
from traceart.basemap.download import DownloadError, download_to, fetch_lock
from traceart.errors import UnavailableError

MANIFEST_NAME = "manifest.json"

_MULTI_TYPES = {
    "Point": "MultiPoint",
    "LineString": "MultiLineString",
    "Polygon": "MultiPolygon",
}


def multi_geometry_type(name: object) -> str:
    """Type de géométrie → son équivalent Multi.

    `promote_to_multi` promeut les *entités*, pas le type déclaré de la
    couche : écrire des `MultiPolygon` dans une couche `Polygon` est
    refusé. Le cas se présente des deux côtés — Natural Earth déclare
    `ne_10m_ocean` en `Polygon` alors qu'il contient des `MultiPolygon`,
    et le pilote OSM annonce `Point` pour une couche promue en
    `MultiPoint`.
    """
    base = str(name)
    prefix = ""
    if base.startswith("3D "):
        prefix, base = "3D ", base[3:]
    return prefix + _MULTI_TYPES.get(base, base)
DOWNLOAD_TIMEOUT_S = 120


class StoreError(UnavailableError):
    """Cache inutilisable, ou données de fond absentes."""


def default_cache_dir() -> Path:
    """Respecte XDG_CACHE_HOME, sinon ~/.cache/traceart."""
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "traceart"


@dataclass(frozen=True, slots=True)
class Entry:
    """Ce que le manifeste retient d'un jeu téléchargé."""

    name: str
    url: str
    sha256: str
    features: int

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "url": self.url,
            "sha256": self.sha256,
            "features": self.features,
        }


class Store:
    """Répertoire de cache : zips bruts, couches FlatGeobuf, manifeste."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else default_cache_dir()
        self.raw_dir = self.root / "raw"
        self.layers_dir = self.root / "layers"

    # ------------------------------------------------------------------ chemins

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST_NAME

    def layer_path(self, dataset: Dataset) -> Path:
        return self.layers_dir / f"{dataset.key}.fgb"

    def has(self, dataset: Dataset) -> bool:
        return self.layer_path(dataset).is_file()

    # ---------------------------------------------------------------- manifeste

    def read_manifest(self) -> dict[str, Entry]:
        if not self.manifest_path.is_file():
            return {}
        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StoreError(f"{self.manifest_path} : manifeste illisible ({exc})") from exc
        return {
            name: Entry(
                name=name,
                url=str(item.get("url", "")),
                sha256=str(item.get("sha256", "")),
                features=int(item.get("features", 0)),
            )
            for name, item in raw.get("datasets", {}).items()
        }

    def write_manifest(self, entries: dict[str, Entry]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            # Tri des clés : le manifeste doit être comparable d'une
            # machine à l'autre.
            "datasets": {name: entries[name].to_json() for name in sorted(entries)},
        }
        self.manifest_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    # ---------------------------------------------------------- téléchargement

    def _download(self, dataset: Dataset) -> tuple[Path, str]:
        """Télécharge le zip et renvoie (chemin, sha256)."""
        target = self.raw_dir / f"{dataset.key}.zip"
        try:
            digest = download_to(dataset.url, target, timeout=DOWNLOAD_TIMEOUT_S)
        except DownloadError as exc:
            raise StoreError(f"{dataset.name} : {exc}") from exc
        return target, digest

    def _convert(self, dataset: Dataset, zip_path: Path) -> int:
        """Convertit le shapefile zippé en FlatGeobuf. Renvoie le nb d'entités."""
        from pyogrio.raw import read, write

        # /vsizip/ lit dans l'archive sans la décompresser sur disque.
        source = f"/vsizip/{zip_path}/{dataset.name}.shp"
        try:
            # force_2d : les Z de Natural Earth ne servent à rien sur une
            # carte plane et alourdissent le FlatGeobuf.
            meta, _fids, geometry, field_data = read(
                source, read_geometry=True, force_2d=True
            )
        except Exception as exc:  # pyogrio remonte des DataSourceError variés
            raise StoreError(f"{dataset.name} : lecture du shapefile ({exc})") from exc

        if geometry is None or len(geometry) == 0:
            raise StoreError(f"{dataset.name} : archive sans géométrie")

        # Natural Earth contient quelques entités à géométrie NULL (des
        # enregistrements purement attributaires). L'index spatial de
        # FlatGeobuf les refuse : on les écarte avant l'écriture.
        keep = np.fromiter(
            (item is not None and len(item) > 0 for item in geometry),
            dtype=bool,
            count=len(geometry),
        )
        if not keep.all():
            geometry = geometry[keep]
            field_data = [column[keep] for column in field_data]
        if len(geometry) == 0:
            raise StoreError(f"{dataset.name} : aucune géométrie exploitable")

        self.layers_dir.mkdir(parents=True, exist_ok=True)
        target = self.layer_path(dataset)
        with tempfile.TemporaryDirectory(dir=self.layers_dir) as tmpdir:
            staging = Path(tmpdir) / target.name
            write(
                str(staging),
                geometry=geometry,
                field_data=field_data,
                fields=meta["fields"],
                geometry_type=multi_geometry_type(meta["geometry_type"]),
                crs=meta["crs"],
                driver="FlatGeobuf",
                promote_to_multi=True,
            )
            shutil.move(str(staging), target)
        return len(geometry)

    def fetch(
        self,
        datasets: list[Dataset] | None = None,
        *,
        force: bool = False,
        on_progress=None,
    ) -> dict[str, Entry]:
        """Télécharge et convertit les jeux manquants.

        `on_progress(dataset, state)` est appelé avec state ∈
        {"skip", "download", "done"} pour l'affichage CLI.
        """
        wanted = list(datasets) if datasets is not None else list(CATALOG)

        # Verrou inter-processus : rien n'empêche par ailleurs le CLI de
        # lancer `data fetch` pendant qu'un serveur web fait la même
        # chose sur le même cache.
        with fetch_lock(self.root):
            entries = self.read_manifest()

            for dataset in wanted:
                if not force and self.has(dataset) and dataset.name in entries:
                    if on_progress:
                        on_progress(dataset, "skip")
                    continue
                if on_progress:
                    on_progress(dataset, "download")
                zip_path, digest = self._download(dataset)
                features = self._convert(dataset, zip_path)
                entries[dataset.name] = Entry(
                    name=dataset.name, url=dataset.url, sha256=digest, features=features
                )
                if on_progress:
                    on_progress(dataset, "done")

            self.write_manifest(entries)
        return entries

    def status(self) -> list[tuple[Dataset, bool, int]]:
        """(jeu, présent, nombre d'entités) pour tout le catalogue."""
        entries = self.read_manifest()
        out = []
        for dataset in CATALOG:
            entry = entries.get(dataset.name)
            out.append((dataset, self.has(dataset), entry.features if entry else 0))
        return out

    def total_size(self) -> int:
        if not self.root.is_dir():
            return 0
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())

    def prune_raw(self) -> int:
        """Supprime les zips sources ; les FlatGeobuf suffisent au rendu."""
        if not self.raw_dir.is_dir():
            return 0
        freed = sum(p.stat().st_size for p in self.raw_dir.glob("*.zip"))
        shutil.rmtree(self.raw_dir)
        return freed


def missing_datasets(store: Store, datasets: list[Dataset]) -> list[Dataset]:
    """Jeux dont l'absence empêche le rendu.

    Les suppléments (`Dataset.optional`) en sont exclus : leur absence
    appauvrit le fond sans le bloquer. La règle vit ici plutôt que chez
    l'appelant, pour qu'aucun appelant ne puisse l'oublier.
    """
    return [d for d in datasets if not d.optional and not store.has(d)]

