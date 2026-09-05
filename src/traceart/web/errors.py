"""Conversion des exceptions du projet en réponses HTTP.

Une seule table, un seul handler. `TraceArtError` couvre toutes les
exceptions du projet (voir `traceart.errors` et l'invariant de test qui
le garantit) : c'est elle qui permet de ne PAS enregistrer `ValueError`
ou `RuntimeError` bruts comme gestionnaires — un bug qui lève un
`ValueError` non lié au projet doit rester un 500, pas devenir un 422
silencieux.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from traceart.errors import MissingDataError, TraceArtError, UnavailableError, UserError
from traceart.render.png import PngUnavailable

# Ordre du plus spécifique au plus général : `MissingDataError` est un
# `UserError`, elle doit être testée avant lui. `isinstance` avec un
# tuple ordonné ne suffit pas à garantir cet ordre — on itère la liste.
_STATUS_TABLE: tuple[tuple[type[TraceArtError], int], ...] = (
    (MissingDataError, 409),
    (PngUnavailable, 501),
    (UnavailableError, 502),
    (UserError, 422),
    (TraceArtError, 500),
)


def status_for(exc: Exception) -> int:
    """Code HTTP pour une exception du projet. 500 si hors taxonomie."""
    if not isinstance(exc, TraceArtError):
        return 500
    for exc_type, status in _STATUS_TABLE:
        if isinstance(exc, exc_type):
            return status
    return 500  # pragma: no cover - inatteignable, TraceArtError clôt la table


def install_error_handlers(app: FastAPI, templates: Jinja2Templates) -> None:
    """Enregistre le handler unique. `TraceArtError` couvre tout le reste
    par la hiérarchie ; Starlette route déjà vers le handler le plus
    spécifique enregistré, mais un seul enregistrement suffit ici parce
    que le choix du code est fait par `status_for`, pas par la classe
    enregistrée."""

    @app.exception_handler(TraceArtError)
    async def _handle(request: Request, exc: TraceArtError) -> HTMLResponse | JSONResponse:
        status = status_for(exc)
        if request.headers.get("HX-Request") == "true":
            return templates.TemplateResponse(
                request,
                "_error.html",
                {"message": str(exc), "missing_data": isinstance(exc, MissingDataError)},
                status_code=status,
            )
        return JSONResponse({"error": str(exc)}, status_code=status)
