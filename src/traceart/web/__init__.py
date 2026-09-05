"""Interface web de TraceArt — façade FastAPI + HTMX au-dessus du cœur.

`create_app` est une factory, pas un singleton de module : chaque appel
construit une application indépendante, ce qui rend le paquet testable
sans état partagé entre les tests (`fastapi.testclient.TestClient` sur
une instance fraîche à chaque fixture).
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from traceart.errors import UnavailableError
from traceart.web.errors import install_error_handlers
from traceart.web.jobs import JobRunner, ThreadJobRunner
from traceart.web.settings import WebSettings

_PACKAGE_DIR = Path(__file__).resolve().parent


class MultiWorkerError(UnavailableError):
    """L'état de l'application (sessions, tâches) est en mémoire et par
    processus : au-delà d'un worker, sessions et progressions se
    perdraient au hasard du routage. Voir README, section Déploiement."""


def _refuse_if_multi_worker() -> None:
    workers = os.environ.get("WEB_CONCURRENCY")
    if workers and int(workers) > 1:
        raise MultiWorkerError(
            f"WEB_CONCURRENCY={workers} détecté : TraceArt web garde son état "
            "en mémoire (sessions, tâches de téléchargement) et ne fonctionne "
            "correctement qu'avec un seul worker. Voir README, section "
            "Déploiement, pour les options au-delà d'un worker."
        )


def create_app(
    settings: WebSettings | None = None, *, jobs: JobRunner | None = None
) -> FastAPI:
    """Construit l'application. `jobs` n'est paramétrable que pour les
    tests (`InlineJobRunner`) — en production, `ThreadJobRunner` seul a
    du sens (voir `web/jobs.py`)."""
    _refuse_if_multi_worker()
    settings = settings or WebSettings.from_env()

    app = FastAPI(title="TraceArt")
    templates = Jinja2Templates(directory=str(_PACKAGE_DIR / "templates"))
    app.state.settings = settings
    app.state.templates = templates
    app.state.jobs = jobs or ThreadJobRunner()

    app.mount(
        "/static", StaticFiles(directory=str(_PACKAGE_DIR / "static")), name="static"
    )
    install_error_handlers(app, templates)

    # Import paresseux : les routes importent `pipeline`/`options`, déjà
    # lourds à charger (numpy, shapely, pyproj) — pas de raison de payer
    # ce coût avant que `create_app` soit réellement appelée.
    from traceart.web import data, routes

    app.include_router(routes.router)
    app.include_router(data.router)
    return app
