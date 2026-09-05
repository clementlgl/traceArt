"""Routes de rendu : page d'accueil, upload, rendu, artefacts.

Les routes de rendu sont déclarées `def`, jamais `async def` : Starlette
les bascule automatiquement dans son threadpool, ce qui libère la boucle
d'événements pendant les 0,3 à 1,1 s d'un rendu. Une `async def` ici
gèlerait toutes les requêtes concurrentes — polls de progression compris
— le temps du rendu. C'est l'erreur la plus probable de ce module.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import APIRouter, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response

from traceart.core.gpx import collect_gpx
from traceart.errors import UserError
from traceart.layers import LAYERS
from traceart.options import build_options, ui_specs
from traceart.pipeline import ASPECT_PRESETS, default_title, run
from traceart.render.png import svg_to_png_bytes
from traceart.render.theme import available_themes, load_theme
from traceart.web.settings import WebSettings
from traceart.web.uploads import (
    new_session_id,
    save,
    session_dir,
    sweep_expired_sessions,
)

router = APIRouter()

_SESSION_COOKIE = "traceart_session"
# Aperçu forcé à cette taille : `Options.size` ne fait pas partie des
# options exposées, le choix de résolution n'existe qu'à l'export.
_PREVIEW_SIZE = "1000px"


def _settings(request: Request) -> WebSettings:
    return request.app.state.settings


def _session_id(request: Request) -> str | None:
    return request.cookies.get(_SESSION_COOKIE)


def _theme_choices() -> list[dict[str, str]]:
    return [
        {"name": name, "label": load_theme(name).label} for name in available_themes()
    ]


def _template_context(request: Request, **extra: object) -> dict[str, object]:
    return {
        "request": request,
        "themes": _theme_choices(),
        "layers": LAYERS,
        "aspects": sorted(ASPECT_PRESETS),
        "ui_specs": ui_specs(),
        **extra,
    }


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> Response:
    settings = _settings(request)
    session_id = _session_id(request) or new_session_id()
    sweep_expired_sessions(settings.work_dir, ttl_s=settings.session_ttl_s)

    files = _session_files(settings, session_id)
    response = request.app.state.templates.TemplateResponse(
        request, "index.html", _template_context(request, files=files)
    )
    if not request.cookies.get(_SESSION_COOKIE):
        response.set_cookie(
            _SESSION_COOKIE, session_id, max_age=int(settings.session_ttl_s), httponly=True
        )
    return response


def _session_files(settings: WebSettings, session_id: str) -> list[str]:
    directory = session_dir(settings.work_dir, session_id)
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.glob("*.gpx"))


@router.post("/upload", response_class=HTMLResponse)
async def upload(request: Request, files: list[UploadFile]) -> Response:
    settings = _settings(request)
    session_id = _session_id(request) or new_session_id()

    payload = [(f.filename, f.file) for f in files]
    try:
        saved = save(
            settings.work_dir, session_id, payload, max_bytes=settings.max_upload_bytes
        )
    finally:
        for f in files:
            await f.close()

    response = request.app.state.templates.TemplateResponse(
        request,
        "_uploaded.html",
        _template_context(request, files=[p.name for p in saved]),
    )
    if not request.cookies.get(_SESSION_COOKIE):
        response.set_cookie(
            _SESSION_COOKIE, session_id, max_age=int(settings.session_ttl_s), httponly=True
        )
    return response


@router.post("/render", response_class=HTMLResponse)
def render(
    request: Request,
    theme: str = Form("light"),
    basemap: str = Form("auto"),
    aspect: str = Form(""),
    layers: list[str] = Form([]),
    annotations: bool = Form(False),
    profile: bool = Form(False),
    stats: bool = Form(True),
    title: str = Form(""),
    subtitle: str = Form(""),
) -> Response:
    settings = _settings(request)
    session_id = _session_id(request)
    if not session_id:
        raise UserError("session inconnue — recharge la page et renvoie ton GPX")

    directory = session_dir(settings.work_dir, session_id)
    gpx_paths = collect_gpx([directory]) if directory.is_dir() else []
    if not gpx_paths:
        raise UserError("aucun GPX pour cette session — envoie un fichier d'abord")

    # `basemap="on"` n'est jamais proposé par le formulaire : ce mode
    # échoue net sur cache incomplet, ce que l'interface ne doit jamais
    # infliger à l'utilisateur.
    if basemap not in {"off", "auto"}:
        basemap = "auto"

    overrides = {
        "theme": theme,
        "basemap": basemap,
        "aspect": aspect or None,
        "layers": ",".join(layers) if layers else "none",
        "show_annotations": annotations,
        "show_profile": profile,
        "show_stats": stats,
        "title": title or None,
        "subtitle": subtitle or None,
        "size": _PREVIEW_SIZE,
        "cache_dir": settings.cache_dir,
    }
    options = build_options({}, overrides)
    result = run(gpx_paths, options)

    title_used = options.title or default_title(result.tracks)
    token = _store_artifact(settings, result.svg)

    return request.app.state.templates.TemplateResponse(
        request,
        "_preview.html",
        _template_context(
            request,
            result=result,
            token=token,
            title=title_used,
            slug=_slug(title_used),
        ),
    )


def _slug(text: str) -> str:
    from traceart.pipeline import slugify

    return slugify(text)


def _artifacts_dir(settings: WebSettings) -> Path:
    directory = settings.work_dir / "artifacts"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _store_artifact(settings: WebSettings, svg: str) -> str:
    """Écrit le SVG rendu, nommé par hash de son contenu.

    Un même rendu (mêmes options, même GPX) retombe sur le même fichier :
    aucun risque de collision entre sessions, et le téléchargement
    réutilise l'aperçu sans re-rendre.
    """
    token = hashlib.sha256(svg.encode("utf-8")).hexdigest()[:24]
    target = _artifacts_dir(settings) / f"{token}.svg"
    if not target.is_file():
        target.write_text(svg, encoding="utf-8")
    return token


@router.get("/artifact/{token}.svg")
def artifact_svg(request: Request, token: str) -> Response:
    path = _artifact_path(request, token)
    return Response(path.read_bytes(), media_type="image/svg+xml")


@router.get("/artifact/{token}.png")
def artifact_png(request: Request, token: str, width: int = 2000) -> Response:
    path = _artifact_path(request, token)
    width = max(200, min(width, 6000))
    data = svg_to_png_bytes(path.read_text(encoding="utf-8"), width_px=width)
    return Response(data, media_type="image/png")


def _artifact_path(request: Request, token: str) -> Path:
    if not token.isalnum():
        raise UserError("artefact introuvable")
    path = _artifacts_dir(_settings(request)) / f"{token}.svg"
    if not path.is_file():
        raise UserError("artefact introuvable ou expiré")
    return path
