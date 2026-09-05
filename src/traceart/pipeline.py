"""Chaîne complète GPX → SVG, utilisable comme bibliothèque.

Le CLI n'est qu'une façade sur ce module : une future interface web
appellera `run` directement, sans passer par un sous-processus.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np
from pyproj import Transformer

from traceart.basemap.catalog import DEFAULT_LAYERS
from traceart.basemap.osm import OsmStore
from traceart.basemap.query import (
    OSM_SCALES,
    BasemapResult,
    build_basemap,
    osm_region_for,
    planned_datasets,
    visible_bounds_wgs84,
)
from traceart.basemap.query import suggest_cities as _suggest_cities_in_bbox
from traceart.basemap.store import Store, default_cache_dir, missing_datasets
from traceart.basemap.tiers import Tier, choose_tier
from traceart.core.clean import CleanConfig, CleanReport, clean_track
from traceart.core.gpx import parse_many
from traceart.core.model import Track
from traceart.core.project import Projector, choose_projection
from traceart.core.project import project_track as _project
from traceart.core.simplify import simplify_track
from traceart.core.stats import TrackStats, compute_stats
from traceart.errors import MissingDataError, UserError
from traceart.render.label import CITY, Label
from traceart.render.layout import Layout, combined_bounds, fit_layout, layout_track
from traceart.render.svg import (
    Annotation,
    RenderRequest,
    annotation_band_height,
    render_svg,
)
from traceart.render.theme import Theme, load_theme

# Tolérance Douglas-Peucker par défaut, en unités de viewBox (grand côté
# 1000). 0,25 unité est invisible à l'œil et retire 90 à 99 % des points
# d'une trace GPS brute.
DEFAULT_TOLERANCE = 0.25

BASEMAP_MODES = ("auto", "on", "off")

# Ratios largeur/hauteur nommés, pour `--aspect`.
ASPECT_PRESETS = {
    "trace": None,
    "a4": 210 / 297,
    "a4-paysage": 297 / 210,
    "a3": 297 / 420,
    "a3-paysage": 420 / 297,
    "a2": 420 / 594,
    "a2-paysage": 594 / 420,
    "square": 1.0,
    "story": 9 / 16,
    "wide": 16 / 9,
}


class PipelineError(UserError):
    """Entrée inutilisable ou options incohérentes."""


def parse_aspect(value: str | None) -> float | None:
    """`a3`, `square`, `16:9` ou un flottant → ratio largeur/hauteur."""
    if value is None or value == "":
        return None
    key = value.strip().lower()
    if key in ASPECT_PRESETS:
        return ASPECT_PRESETS[key]
    if ":" in key:
        left, _, right = key.partition(":")
        try:
            w, h = float(left), float(right)
        except ValueError as exc:
            raise PipelineError(f"ratio invalide : {value!r}") from exc
        if h <= 0 or w <= 0:
            raise PipelineError(f"ratio invalide : {value!r}")
        return w / h
    try:
        ratio = float(key)
    except ValueError as exc:
        raise PipelineError(
            f"ratio invalide : {value!r} (attendu {', '.join(ASPECT_PRESETS)}, 16:9 ou 0.707)"
        ) from exc
    if ratio <= 0:
        raise PipelineError(f"ratio invalide : {value!r}")
    return ratio


def parse_label(value: str) -> tuple[str, float, float]:
    """`"Nom:lon,lat"` → `(nom, lon, lat)`, pour `--label` en CLI.

    Coordonnées explicites uniquement — pas de recherche par nom : le
    CLI reste utilisable hors ligne. La recherche par nom (Nominatim)
    est une commodité propre à l'interface web.
    """
    name, sep, coords = value.partition(":")
    if not sep:
        raise PipelineError(
            f"label invalide : {value!r} (attendu \"Nom:lon,lat\")"
        )
    lon_text, comma, lat_text = coords.partition(",")
    if not comma:
        raise PipelineError(f"label invalide : {value!r} (attendu \"Nom:lon,lat\")")
    try:
        lon, lat = float(lon_text), float(lat_text)
    except ValueError as exc:
        raise PipelineError(f"coordonnées invalides dans {value!r}") from exc
    if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
        raise PipelineError(f"coordonnées hors plage dans {value!r}")
    name = name.strip()
    if not name:
        raise PipelineError(f"label sans nom : {value!r}")
    return name, lon, lat


@dataclass(frozen=True, slots=True)
class Options:
    theme: str = "light"
    # Surcharge ponctuelle de la couleur de trace d'un thème par ailleurs
    # inchangé (voir `Theme.with_trace_color`). None = couleur(s) du
    # thème telles quelles.
    trace_color: str | None = None
    projection: str = "auto"
    tolerance: float = DEFAULT_TOLERANCE
    margin: float | None = None
    aspect: float | None = None
    size: str = "2000px"
    clean: CleanConfig = field(default_factory=CleanConfig)
    title: str | None = None
    subtitle: str | None = None
    show_stats: bool = True
    show_profile: bool = False
    # Carte nue par défaut : ni titre, ni date, ni mesures. Le bandeau
    # s'active à la demande (`--annotations`).
    show_annotations: bool = False
    enable_clean: bool = True
    # "auto" : fond dessiné si le cache est rempli, ignoré sinon — pour
    # qu'un premier lancement produise quelque chose sans téléchargement.
    # "on" exige les données, "off" les ignore.
    basemap: str = "auto"
    layers: tuple[str, ...] = DEFAULT_LAYERS
    bleed: bool = True
    cache_dir: Path | None = None
    # Extraits OSM importés (Tier B). Coupé, le fond reste sur Natural
    # Earth même si une région est disponible.
    use_osm: bool = True


@dataclass(slots=True)
class Result:
    """Sortie du pipeline : le SVG et tout ce qui a servi à le produire."""

    svg: str
    tracks: list[Track]
    stats: list[TrackStats]
    reports: list[CleanReport]
    layout: Layout
    theme: Theme
    projection: str
    points_rendered: int
    tier: Tier | None = None
    basemap_note: str | None = None
    basemap_source: str | None = None
    # Jeux Natural Earth absents du cache, quand c'est pour ça qu'une
    # couche a été omise. Vide si tout était présent, ou si le fond a
    # été désactivé par choix (`--basemap off`).
    basemap_missing: tuple[str, ...] = ()
    # Vrai si un extrait OSM aurait pu s'appliquer (palier 10m, `--osm`
    # actif) mais qu'aucun n'importé ne couvre l'emprise. Le rendu s'est
    # replié sur Natural Earth sans broncher ; un appelant peut s'en
    # servir pour proposer le téléchargement.
    osm_missing: bool = False
    # Noms de `extra_labels` tombés hors du cadre visible : pas dessinés
    # (aucun clip-path ne protège le SVG d'un texte égaré), mais signalés
    # pour que l'appelant puisse le dire à l'utilisateur.
    dropped_labels: tuple[str, ...] = ()

    @property
    def points_source(self) -> int:
        return sum(r.points_in for r in self.reports) if self.reports else self.points_rendered


# Dossiers d'organisation, sans valeur de titre : on remonte d'un cran.
_GENERIC_DIRS = frozenset(
    {
        "data", "export", "exports", "files", "gpx", "gpx_input", "gpxs", "in",
        "input", "inputs", "out", "output", "outputs", "svg", "svg_output",
        "traces", "tracks",
    }
)


# Certains traceurs (GeoRide, par exemple) nomment la trace par sa date.
# « 10/05/24 » fait un mauvais titre de poster : le nom du fichier vaut
# mieux, et la date apparaît de toute façon en sous-titre.
_DATE_LIKE = re.compile(r"^[\d\s/.:_-]+$")


def _useful_name(track: Track) -> str:
    name = (track.name or "").strip()
    if name and not _DATE_LIKE.match(name):
        return name
    return track.source.stem if track.source else name or "trace"


def default_title(tracks: list[Track]) -> str:
    """Titre déduit des sources : nom de trace, sinon dossier parent utile."""
    if len(tracks) == 1:
        return _prettify(_useful_name(tracks[0]))
    parents = {t.source.parent for t in tracks if t.source}
    if len(parents) != 1:
        return "TraceArt"
    folder = next(iter(parents))
    while folder.name.lower() in _GENERIC_DIRS and folder.parent != folder:
        folder = folder.parent
    return _prettify(folder.name) if folder.name else "TraceArt"


def _prettify(raw: str) -> str:
    """`PARTIE_1_LES_ALPES` → `Partie 1 Les Alpes`."""
    cleaned = raw.replace("_", " ").replace("-", " ").strip()
    collapsed = " ".join(cleaned.split())
    return collapsed.title() if collapsed.isupper() or collapsed.islower() else collapsed


def slugify(text: str) -> str:
    """Nom de fichier sûr, dérivé d'un titre : espaces et diacritiques
    conservés en NFC, tout le reste réduit à des tirets.

    Partagé par le CLI (nom du SVG écrit) et par l'interface web (nom du
    fichier proposé au téléchargement) — un seul endroit qui sait ce
    qu'est un nom de fichier valide pour TraceArt.
    """
    slug = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    slug = re.sub(r"[\s_-]+", "-", slug)
    return slug or "traceart"


def _default_subtitle(stats: list[TrackStats]) -> str | None:
    from traceart.render.svg import _date_range

    return _date_range(stats)


def _order_chronologically(
    tracks: list[Track], stats: list[TrackStats]
) -> tuple[list[Track], list[TrackStats]]:
    """Réordonne les traces par date de départ quand toutes en ont une.

    Sans horodatage, l'ordre alphabétique des fichiers est conservé : il
    est déterministe, et `PARTIE_1/2/3` se trie correctement. Mais dès que
    les dates existent elles font foi — le profil d'élévation d'un voyage
    de plusieurs jours serait sinon assemblé dans le désordre.
    """
    if len(tracks) < 2 or any(s.start_time is None for s in stats):
        return tracks, stats
    order = sorted(range(len(stats)), key=lambda i: stats[i].start_time)
    return [tracks[i] for i in order], [stats[i] for i in order]


def _empty_basemap(
    note: str | None = None,
    *,
    tier: Tier | None = None,
    missing: tuple[str, ...] = (),
    osm_missing: bool = False,
) -> BasemapResult:
    """Fond vide, avec le message expliquant pourquoi le cas échéant.

    `tier` reste `None` seulement quand le palier n'a jamais été calculé
    (fond désactivé, ou aucune couche demandée) : dans tous les autres
    cas de repli, l'appelant connaît déjà le palier et doit le
    transmettre, sinon un futur bouton « télécharger » ne saurait pas
    quelle résolution proposer.
    """
    return BasemapResult(
        layers={}, labels=[], tier=tier, note=note, missing=missing, osm_missing=osm_missing
    )


def _build_basemap(opts: Options, layout, projector: Projector) -> BasemapResult:
    """Fond de carte selon le mode demandé.

    La note portée par le résultat explique ce qui a été laissé de côté :
    la vérification du cache se fait couche par couche, une seule
    absente ne doit pas emporter les autres.
    """
    if opts.basemap not in BASEMAP_MODES:
        raise PipelineError(
            f"mode de fond inconnu : {opts.basemap!r} (attendu {BASEMAP_MODES})"
        )
    if opts.basemap == "off" or not opts.layers:
        return _empty_basemap()

    store = Store(opts.cache_dir)
    osm_store = OsmStore(opts.cache_dir or default_cache_dir()) if opts.use_osm else None

    # Le palier de zoom décide de la résolution : inutile d'exiger le 10m
    # complet pour rendre une trace transcontinentale en 110m.
    bounds = visible_bounds_wgs84(layout, projector, bleed=opts.bleed)
    tier = choose_tier(bounds)
    region = osm_region_for(osm_store, bounds, tier)
    # Le palier justifie un extrait OSM et `--osm` est actif, mais aucun
    # extrait importé ne couvre l'emprise : silencieux pour le rendu
    # (Natural Earth prend le relais), mais à signaler à l'appelant, qui
    # peut proposer le téléchargement plutôt que de laisser passer.
    osm_missing = opts.use_osm and tier.scale in OSM_SCALES and region is None

    # Vérification couche par couche. Globalement, un seul jeu manquant
    # emporterait tout le fond : demander `roads` sans `ne_10m_roads` en
    # cache faisait disparaître jusqu'aux frontières et aux noms de pays.
    usable: list[str] = []
    absent: list = []
    dropped: list[str] = []
    for layer in opts.layers:
        needed = planned_datasets(
            (layer,), tier=tier, store=store, osm_store=osm_store, region=region
        )
        gaps = missing_datasets(store, needed)
        if gaps:
            dropped.append(layer)
            absent.extend(gaps)
        else:
            usable.append(layer)

    hint = f"`traceart data fetch --scale {tier.scale}`"
    # Nommer les jeux manquants dans tous les cas, pas seulement pour le
    # message : un appelant qui veut proposer un téléchargement (le web,
    # notamment) a besoin de la liste elle-même, pas d'une phrase à
    # reparser.
    missing_names = tuple(sorted({d.name for d in absent})) if absent else ()
    if missing_names and opts.basemap == "on":
        # MissingDataError plutôt qu'une simple PipelineError : un
        # appelant doit pouvoir répondre par une invitation à télécharger
        # plutôt que par un message d'erreur de saisie.
        raise MissingDataError(
            f"couche(s) {', '.join(dropped)} : {', '.join(missing_names)} absent(s) "
            f"du cache — lance {hint}",
            datasets=missing_names,
            scale=tier.scale,
            layers=tuple(dropped),
        )

    if not usable:
        # Mode auto : on rend la trace seule plutôt que d'échouer.
        return _empty_basemap(
            f"fond ignoré, rien en cache : {hint}",
            tier=tier,
            missing=missing_names,
            osm_missing=osm_missing,
        )

    note = (
        f"couche(s) {', '.join(dropped)} ignorée(s), absentes du cache : {hint}"
        if dropped
        else None
    )
    result = build_basemap(
        layout,
        projector,
        layers=tuple(usable),
        store=store,
        bleed=opts.bleed,
        tier=tier,
        osm_store=osm_store,
    )
    return replace(result, note=note, missing=missing_names, osm_missing=osm_missing)


def _resolve_extra_labels(
    entries: Sequence[tuple[str, float, float]],
    projector: Projector,
    layout: Layout,
) -> tuple[list[Label], tuple[str, ...]]:
    """Convertit des (nom, lon, lat) en `Label` prêts à dessiner.

    Ignore toujours le filtre de rang/population des labels automatiques
    — c'est tout le sens de la fonctionnalité : l'utilisateur a demandé
    ce nom précisément parce qu'il n'apparaîtrait pas de lui-même. En
    revanche, un nom hors du cadre visible n'est pas dessiné : aucun
    `clip-path` ne protège le SVG produit, un `<text>` égaré loin de la
    trace ne serait pas juste inutile, il traînerait dans le document.
    """
    if not entries:
        return [], ()

    min_lon, min_lat, max_lon, max_lat = visible_bounds_wgs84(layout, projector)
    transformer = Transformer.from_crs("EPSG:4326", projector.crs, always_xy=True)

    labels: list[Label] = []
    dropped: list[str] = []
    for name, lon, lat in entries:
        if not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat):
            dropped.append(name)
            continue
        x, y = transformer.transform(lon, lat)
        placed = layout.apply(np.asarray([[x, y]]))
        labels.append(Label(float(placed[0, 0]), float(placed[0, 1]), name, kind=CITY))
    return labels, tuple(dropped)


def run(
    paths: list[str | Path],
    options: Options | None = None,
    *,
    extra_labels: Sequence[tuple[str, float, float]] = (),
) -> Result:
    """Exécute le pipeline sur un ou plusieurs GPX rendus dans un même cadre.

    `extra_labels` : villes ajoutées manuellement, en `(nom, lon, lat)` —
    dessinées quel que soit le palier de zoom, contrairement aux labels
    automatiques du fond de carte (voir `_resolve_extra_labels`).
    """
    opts = options or Options()
    if not paths:
        raise PipelineError("aucun fichier GPX fourni")

    tracks = parse_many(paths)
    theme = load_theme(opts.theme)
    if opts.trace_color:
        theme = theme.with_trace_color(opts.trace_color)

    reports: list[CleanReport] = []
    if opts.enable_clean:
        cleaned = []
        for track in tracks:
            new_track, report = clean_track(track, opts.clean)
            if new_track.is_empty:
                raise PipelineError(
                    f"{track.label} : plus aucun point après nettoyage "
                    "(essaie --no-clean ou augmente --max-speed)"
                )
            cleaned.append(new_track)
            reports.append(report)
        tracks = cleaned

    # Mesures avant simplification : Douglas-Peucker raccourcit la
    # distance et écrête le dénivelé.
    stats = [compute_stats(t) for t in tracks]
    tracks, stats = _order_chronologically(tracks, stats)

    annotation = Annotation(
        title=(opts.title if opts.title is not None else default_title(tracks))
        if opts.show_annotations
        else None,
        subtitle=(opts.subtitle if opts.subtitle is not None else _default_subtitle(stats))
        if opts.show_annotations
        else None,
        show_stats=opts.show_annotations and opts.show_stats,
        # Indépendant du bandeau texte : un profil d'élévation seul sous
        # une carte nue est une composition légitime.
        show_profile=opts.show_profile,
    )

    projector = choose_projection(tracks, opts.projection)
    projected = [_project(t, projector) for t in tracks]

    margin = opts.margin if opts.margin is not None else theme.num("page.margin", 56.0)
    layout = fit_layout(
        combined_bounds(projected),
        long_edge=theme.num("page.long_edge", 1000.0),
        margin=margin,
        annotation_band=annotation_band_height(theme, annotation),
        aspect=opts.aspect,
    )

    basemap = _build_basemap(opts, layout, projector)
    custom_labels, dropped_labels = _resolve_extra_labels(extra_labels, projector, layout)
    # Une ville ajoutée à la main peut aussi passer le filtre du palier
    # automatique : sans ce filtre, elle serait dessinée deux fois au
    # même endroit.
    auto_names = {lbl.text for lbl in basemap.labels}
    custom_labels = [lbl for lbl in custom_labels if lbl.text not in auto_names]

    placed = [layout_track(t, layout) for t in projected]
    simplified = [simplify_track(t, opts.tolerance) for t in placed]
    drawable = [t for t in simplified if not t.is_empty]
    if not drawable:
        raise PipelineError("plus rien à dessiner : tolérance de simplification trop grande ?")

    request = RenderRequest(
        tracks=drawable,
        layout=layout,
        theme=theme,
        stats=stats,
        basemap=basemap.layers,
        annotation=annotation,
        labels=[*basemap.labels, *custom_labels],
    )
    svg = render_svg(request, size=opts.size)

    return Result(
        svg=svg,
        tracks=drawable,
        stats=stats,
        reports=reports,
        layout=layout,
        theme=theme,
        projection=projector.definition,
        points_rendered=sum(t.n_points for t in drawable),
        dropped_labels=dropped_labels,
        tier=basemap.tier,
        basemap_note=basemap.note,
        basemap_source=basemap.source,
        basemap_missing=basemap.missing,
        osm_missing=basemap.osm_missing,
    )


def suggest_cities(
    paths: list[str | Path], options: Options | None = None, *, limit: int = 8
) -> list[tuple[str, float, float]]:
    """Plus grosses villes visibles pour ce cadre, sans filtre de palier.

    Ne fait que la moitié de `run()` (jusqu'à la mise en page) : pas
    besoin de simplifier la trace ni de produire le SVG pour proposer
    des ajouts manuels. Sert l'interface web (villes suggérées à
    l'upload) — voir `basemap.query.suggest_cities` pour la lecture des
    données locales.
    """
    opts = options or Options()
    if not paths:
        return []
    tracks = parse_many(paths)
    if opts.enable_clean:
        tracks = [clean_track(t, opts.clean)[0] for t in tracks]
        tracks = [t for t in tracks if not t.is_empty]
    if not tracks:
        return []

    theme = load_theme(opts.theme)
    projector = choose_projection(tracks, opts.projection)
    projected = [_project(t, projector) for t in tracks]
    margin = opts.margin if opts.margin is not None else theme.num("page.margin", 56.0)
    layout = fit_layout(
        combined_bounds(projected),
        long_edge=theme.num("page.long_edge", 1000.0),
        margin=margin,
        aspect=opts.aspect,
    )

    bbox = visible_bounds_wgs84(layout, projector, bleed=opts.bleed)
    tier = choose_tier(bbox)
    store = Store(opts.cache_dir)
    osm_store = OsmStore(opts.cache_dir or default_cache_dir()) if opts.use_osm else None
    return _suggest_cities_in_bbox(bbox, tier, store=store, osm_store=osm_store, limit=limit)


def run_each(
    paths: list[str | Path],
    options: Options | None = None,
    *,
    extra_labels: Sequence[tuple[str, float, float]] = (),
) -> list[tuple[Path, Result]]:
    """Un rendu indépendant par fichier — cadrage propre à chaque trace."""
    opts = options or Options()
    # Le titre par défaut doit venir du fichier courant, pas du lot.
    return [
        (Path(path), run([path], replace(opts, title=opts.title), extra_labels=extra_labels))
        for path in sorted(paths, key=str)
    ]
