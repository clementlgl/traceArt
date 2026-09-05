"""Bouton « télécharger l'extrait OSM pour cette carte » (`/data/fetch/
osm/auto`) : résolution automatique de la région Geofabrik depuis le GPX
de la session, sans que l'utilisateur ait à en connaître le chemin."""

from __future__ import annotations

import json

from tests.conftest import make_gpx, trkpt
from traceart.basemap.osm import OsmStore

_GPX = make_gpx(
    "<trk><trkseg>"
    + "".join(trkpt(lon, lat) for lon, lat in ((3.2, 44.0), (3.5, 44.15), (3.8, 44.1)))
    + "</trkseg></trk>"
)

# Volontairement très large : ne pas dépendre de la marge exacte de
# `OsmStore.bbox_around`, seulement du fait qu'elle reste dans un cadre
# raisonnable autour de la trace.
_WIDE_CATALOG = {
    "type": "FeatureCollection",
    "features": [
        {
            "properties": {
                "id": "large-region",
                "parent": "europe",
                "name": "Grande région",
                "urls": {
                    "pbf": "https://download.geofabrik.de/europe/large-region-latest.osm.pbf"
                },
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [[-10.0, -10.0], [20.0, -10.0], [20.0, 60.0], [-10.0, 60.0], [-10.0, -10.0]]
                ],
            },
        }
    ],
}


def _seed_catalog(cache_dir) -> None:
    store = OsmStore(cache_dir)
    store.root.mkdir(parents=True, exist_ok=True)
    store.catalog_path.write_text(json.dumps(_WIDE_CATALOG), encoding="utf-8")


def test_fetch_osm_auto_resolves_region_and_downloads(client, store, monkeypatch):
    _seed_catalog(store.root)

    calls: dict[str, object] = {}

    def fake_download(self, region_path, *, on_progress=None):
        calls["region"] = region_path
        target = self.root / "fake.osm.pbf"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
        return target, "deadbeef"

    def fake_import(self, pbf, *, slug=None, sha256="", bbox=None, on_progress=None):
        calls["slug"] = slug
        calls["bbox"] = bbox
        from datetime import UTC, datetime

        from traceart.basemap.osm import OsmRegion

        return OsmRegion(
            slug=slug or "fake",
            source=str(pbf),
            sha256=sha256,
            bounds=(0, 0, 1, 1),
            counts={},
            imported=datetime.now(UTC).isoformat(),
        )

    monkeypatch.setattr(OsmStore, "download", fake_download)
    monkeypatch.setattr(OsmStore, "import_extract", fake_import)

    client.post("/upload", files={"files": ("cevennes.gpx", _GPX.encode())})
    r = client.post("/data/fetch/osm/auto")
    assert r.status_code == 200
    assert "Terminé" in r.text
    assert calls["region"] == "large-region"
    assert calls["slug"] == "large-region"
    assert calls["bbox"] is not None


def test_fetch_osm_auto_without_session_gpx_fails_clearly(client):
    r = client.post("/data/fetch/osm/auto")
    assert r.status_code == 422
    assert "GPX" in r.text


def test_fetch_osm_auto_no_covering_region_fails_clearly(client, store):
    _seed_catalog(store.root)
    # Catalogue vide : rien ne peut couvrir l'emprise.
    OsmStore(store.root).catalog_path.write_text(
        json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8"
    )
    client.post("/upload", files={"files": ("cevennes.gpx", _GPX.encode())})
    r = client.post("/data/fetch/osm/auto")
    assert r.status_code == 422
    assert "aucune région" in r.text.lower()
