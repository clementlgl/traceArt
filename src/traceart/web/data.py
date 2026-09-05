"""Téléchargement des données de fond depuis l'interface web.

Seule cette route déclenche un accès réseau ou une écriture dans le
cache partagé — tout le reste (`routes.py`) ne fait que lire le cache et
écrire dans le `work_dir` de session. Un unique job à la fois : voir
`web/jobs.py` pour pourquoi (le cache Natural Earth n'est pas conçu pour
des écritures concurrentes).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from traceart.basemap.catalog import CATALOG
from traceart.basemap.osm import OsmStore, slugify_region
from traceart.basemap.store import Store
from traceart.core.gpx import collect_gpx, parse_many
from traceart.errors import UserError
from traceart.render.layout import combined_bounds
from traceart.web.jobs import Job, JobBusyError, Report
from traceart.web.settings import WebSettings
from traceart.web.uploads import session_dir

router = APIRouter(prefix="/data")

# En-dessous de ce seuil, on refuse un import OSM plutôt que de risquer
# de saturer le disque en cours de route (le pilote OSM de GDAL construit
# un index de nœuds temporaire qui peut peser plusieurs Go).
_MIN_FREE_BYTES_FOR_OSM = 10 * 1024**3


def _settings(request: Request) -> WebSettings:
    return request.app.state.settings


def _templates(request: Request):
    return request.app.state.templates


def _job_fragment(request: Request, job: Job) -> Response:
    return _templates(request).TemplateResponse(
        request, "_job.html", {"request": request, "job": job}
    )


@router.get("", response_class=HTMLResponse)
def status(request: Request) -> Response:
    settings = _settings(request)
    ne_store = Store(settings.cache_dir)
    osm_store = OsmStore(settings.cache_dir)
    return _templates(request).TemplateResponse(
        request,
        "data.html",
        {
            "request": request,
            "allow_fetch": settings.allow_fetch,
            "ne_status": ne_store.status(),
            "ne_size": ne_store.total_size(),
            "osm_regions": osm_store.regions(),
            "osm_size": osm_store.total_size(),
        },
    )


@router.post("/fetch/natural-earth", response_class=HTMLResponse)
def fetch_natural_earth(request: Request, scale: str = Form("10m")) -> Response:
    settings = _settings(request)
    if not settings.allow_fetch:
        raise UserError(
            "téléchargement désactivé sur ce serveur — "
            f"lance `traceart data fetch --scale {scale}` toi-même"
        )
    if scale not in {"10m", "50m", "110m"}:
        raise UserError(f"échelle inconnue : {scale!r}")

    datasets = [d for d in CATALOG if d.scale == scale]

    def work(report: Report) -> None:
        store = Store(settings.cache_dir)
        total = len(datasets)
        done = 0

        def on_progress(dataset, state: str) -> None:
            nonlocal done
            if state == "download":
                report(done / total, f"{dataset.name} ({done + 1}/{total})")
            elif state in {"done", "skip"}:
                done += 1
                report(done / total, f"{done}/{total} jeux traités")

        store.fetch(datasets, on_progress=on_progress)

    try:
        job = request.app.state.jobs.submit("natural-earth", work)
    except JobBusyError as exc:
        return _job_fragment(request, exc.current)
    return _job_fragment(request, job)


@router.post("/fetch/osm", response_class=HTMLResponse)
def fetch_osm(
    request: Request,
    region: str = Form(...),
    use_session_gpx: bool = Form(False),
) -> Response:
    settings = _settings(request)
    if not settings.allow_fetch:
        raise UserError(
            "téléchargement désactivé sur ce serveur — "
            f"lance `traceart data osm fetch {region}` toi-même"
        )

    probe = settings.cache_dir if settings.cache_dir.exists() else Path.home()
    free = shutil.disk_usage(probe).free
    if free < _MIN_FREE_BYTES_FOR_OSM:
        raise UserError(
            f"disque trop plein ({free / 1e9:.1f} Go libres, "
            f"{_MIN_FREE_BYTES_FOR_OSM / 1e9:.0f} Go requis) pour importer un "
            "extrait OSM — l'import construit un index temporaire volumineux"
        )

    bbox = None
    if use_session_gpx:
        session_id = request.cookies.get("traceart_session")
        directory = session_dir(settings.work_dir, session_id) if session_id else None
        if directory and directory.is_dir():
            gpx_paths = collect_gpx([directory])
            tracks = parse_many([str(p) for p in gpx_paths])
            bbox = OsmStore.bbox_around(combined_bounds(tracks))

    def work(report: Report) -> None:
        store = OsmStore(settings.cache_dir)
        report(-1.0, f"téléchargement de {region}…")
        pbf, digest = store.download(
            region,
            on_progress=lambda seen, total: report(
                seen / total if total else -1.0,
                f"{seen / 1e6:.0f} / {total / 1e6:.0f} Mo" if total else f"{seen / 1e6:.0f} Mo",
            ),
        )
        try:
            report(-1.0, "import : cours d'eau et routes")
            store.import_extract(
                pbf,
                slug=slugify_region(region),
                sha256=digest,
                bbox=bbox,
                on_progress=lambda stage: report(-1.0, f"import : {stage}"),
            )
        finally:
            pbf.unlink(missing_ok=True)

    try:
        job = request.app.state.jobs.submit("osm", work)
    except JobBusyError as exc:
        return _job_fragment(request, exc.current)
    return _job_fragment(request, job)


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_status(request: Request, job_id: str) -> Response:
    job = request.app.state.jobs.get(job_id)
    if job is None:
        raise UserError("tâche inconnue ou expirée")
    return _job_fragment(request, job)
