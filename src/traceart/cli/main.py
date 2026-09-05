"""Interface en ligne de commande, sur Click.

`traceart trace.gpx` suffit : la sous-commande `render` est implicite
quand le premier argument est un chemin.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from traceart import __version__
from traceart.basemap.catalog import AVAILABLE_LAYERS, CATALOG, DEFAULT_LAYERS, datasets_for
from traceart.basemap.osm import OsmError, OsmStore, geofabrik_url, slugify_region
from traceart.basemap.store import Store, default_cache_dir
from traceart.cli.config import find_config, load_config, resolve
from traceart.core.clean import CleanConfig
from traceart.core.gpx import GpxError
from traceart.pipeline import (
    DEFAULT_TOLERANCE,
    Options,
    PipelineError,
    Result,
    default_title,
    parse_aspect,
    run,
    run_each,
)
from traceart.render.png import PngUnavailable, svg_to_png, target_width_px
from traceart.render.theme import ThemeError, available_themes, load_theme

console = Console()
err_console = Console(stderr=True)

COMMANDS = {"render", "info", "themes", "version", "data"}

# Types d'arguments réutilisés : `path_type=Path` évite de reconvertir
# des chaînes à la main dans chaque commande.
PATH = click.Path(path_type=Path)
EXISTING_PATH = click.Path(exists=True, path_type=Path)


@click.group(help="Transforme un GPX en carte dessinée minimaliste (SVG, PNG optionnel).")
@click.version_option(__version__, "--version", prog_name="traceart")
def app() -> None:
    pass


# --------------------------------------------------------------------- outils


def _slugify(text: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    slug = re.sub(r"[\s_-]+", "-", slug)
    return slug or "traceart"


def _parse_layers(raw: object) -> tuple[str, ...]:
    """`"water,rivers"` ou `["water", "rivers"]` → tuple validé."""
    if raw is None:
        return DEFAULT_LAYERS
    if isinstance(raw, str):
        names = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        names = [str(part).strip() for part in raw]
    else:
        raise PipelineError(f"valeur de --layers invalide : {raw!r}")
    names = [n for n in names if n]
    if names == ["none"] or not names:
        return ()
    unknown = [n for n in names if n not in AVAILABLE_LAYERS]
    if unknown:
        raise PipelineError(
            f"couche(s) inconnue(s) {unknown} (disponibles : {', '.join(AVAILABLE_LAYERS)})"
        )
    # dict.fromkeys : dédoublonne en gardant l'ordre demandé.
    return tuple(dict.fromkeys(names))


def _collect_gpx(inputs) -> list[Path]:
    """Développe les dossiers en fichiers `.gpx` (récursif, ordre stable)."""
    found: list[Path] = []
    for raw in inputs:
        item = Path(raw)
        if item.is_dir():
            found.extend(sorted(item.rglob("*.gpx")))
        elif item.is_file():
            found.append(item)
        else:
            raise PipelineError(f"chemin introuvable : {item}")
    # dict.fromkeys : dédoublonne sans perdre l'ordre.
    unique = list(dict.fromkeys(p.resolve() for p in found))
    if not unique:
        raise PipelineError("aucun fichier .gpx trouvé dans les chemins donnés")
    return unique


def _report_table(result: Result, out_paths: list[Path]) -> Table:
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="dim")
    table.add_column()

    total = sum(s.distance_m for s in result.stats)
    ascent = sum(s.ascent_m for s in result.stats)
    table.add_row("traces", str(len(result.tracks)))
    table.add_row("distance", f"{total / 1000:.1f} km")
    if ascent > 0:
        table.add_row("dénivelé +", f"{ascent:.0f} m")
    if result.reports:
        removed = sum(r.removed for r in result.reports)
        table.add_row("nettoyage", f"−{removed} points")
    reduction = (
        100.0 * (1 - result.points_rendered / result.points_source)
        if result.points_source
        else 0.0
    )
    table.add_row(
        "points", f"{result.points_source} → {result.points_rendered} (−{reduction:.1f} %)"
    )
    table.add_row("cadre", f"{result.layout.width:.0f} × {result.layout.height:.0f}")
    table.add_row("thème", result.theme.label)
    if result.tier:
        table.add_row("fond", f"{result.tier.name} · {result.basemap_source}")
    if result.basemap_note:
        table.add_row("fond", f"[yellow]{result.basemap_note}[/yellow]")
    for path in out_paths:
        table.add_row("écrit", str(path))
    return table


def _write_outputs(
    result: Result,
    out_svg: Path,
    *,
    png: bool,
    size: str,
    dpi: int,
    png_scale: float,
) -> list[Path]:
    out_svg.parent.mkdir(parents=True, exist_ok=True)
    out_svg.write_text(result.svg, encoding="utf-8")
    written = [out_svg]
    if png:
        width_px = target_width_px(size, dpi=dpi, scale=png_scale)
        written.append(svg_to_png(result.svg, out_svg.with_suffix(".png"), width_px=width_px))
    return written


# -------------------------------------------------------------------- render


@app.command()
@click.argument("inputs", nargs=-1, required=True, type=PATH)
@click.option("-o", "--out", type=PATH, help="Fichier SVG de sortie (une seule sortie).")
@click.option("--out-dir", type=PATH, help="Dossier de sortie. Défaut : ./out")
@click.option("--theme", help="Nom de thème livré ou chemin .toml.")
@click.option(
    "--separate/--combine",
    default=False,
    help="Une image par GPX, ou tous dans un cadre.",
)
@click.option("--projection", help="auto | webmercator | utm | EPSG:xxxx")
@click.option(
    "--tolerance", type=float, help="Tolérance Douglas-Peucker, en unités de viewBox."
)
@click.option("--aspect", help="trace | a3 | a4 | square | 16:9 | 0.707")
@click.option("--margin", type=float, help="Marge, unités viewBox.")
@click.option("--size", help="Grand côté du document : 2000px, 297mm…")
@click.option("--title", help="Titre. Défaut : nom du GPX.")
@click.option("--subtitle", help="Sous-titre.")
@click.option(
    "--annotations/--no-annotations",
    default=None,
    help="Bandeau titre, date et mesures. Désactivé par défaut.",
)
@click.option(
    "--profile/--no-profile", default=None, help="Profil d'élévation en pied de page."
)
@click.option(
    "--stats/--no-stats", default=None, help="Distance, dénivelé et durée dans le bandeau."
)
@click.option(
    "--clean/--no-clean",
    default=None,
    help="Nettoyage aberrations/pauses/doublons.",
)
@click.option("--max-speed", type=float, help="Vitesse max plausible (km/h).")
@click.option("--pause-radius", type=float, help="Rayon d'immobilité (m).")
@click.option("--basemap", help="auto (défaut) | on (exige le cache) | off")
@click.option("--layers", help=f"Couches de fond : {','.join(AVAILABLE_LAYERS)}")
@click.option(
    "--bleed/--no-bleed",
    default=None,
    help="Fond à fond perdu, ou contenu dans les marges.",
)
@click.option(
    "--osm/--no-osm", default=None, help="Utiliser les extraits OSM importés (Tier B)."
)
@click.option("--cache-dir", type=PATH, help="Cache des données de fond.")
@click.option("--png/--no-png", default=False, help="Exporter aussi un PNG.")
@click.option("--dpi", type=int, help="DPI du PNG si --size est en mm/cm/in.")
@click.option("--png-scale", type=float, help="Facteur du PNG si --size est en px.")
@click.option("--config", type=PATH, help="Fichier traceart.toml explicite.")
def render(
    inputs,
    out,
    out_dir,
    theme,
    separate,
    projection,
    tolerance,
    aspect,
    margin,
    size,
    title,
    subtitle,
    annotations,
    profile,
    stats,
    clean,
    max_speed,
    pause_radius,
    basemap,
    layers,
    bleed,
    osm,
    cache_dir,
    png,
    dpi,
    png_scale,
    config,
) -> None:
    """Rend un ou plusieurs GPX en carte SVG."""
    cfg = load_config(find_config(config))

    clean_cfg = CleanConfig(
        max_speed_kmh=float(resolve(cfg, "clean", "max_speed_kmh", max_speed, 300.0)),
        pause_radius_m=float(resolve(cfg, "clean", "pause_radius_m", pause_radius, 12.0)),
    )
    options = Options(
        theme=str(resolve(cfg, "defaults", "theme", theme, "light")),
        projection=str(resolve(cfg, "defaults", "projection", projection, "auto")),
        tolerance=float(resolve(cfg, "defaults", "tolerance", tolerance, DEFAULT_TOLERANCE)),
        margin=resolve(cfg, "defaults", "margin", margin, None),
        aspect=parse_aspect(resolve(cfg, "defaults", "aspect", aspect, None)),
        size=str(resolve(cfg, "defaults", "size", size, "2000px")),
        clean=clean_cfg,
        title=title,
        subtitle=subtitle,
        show_stats=bool(resolve(cfg, "annotations", "stats", stats, True)),
        show_profile=bool(resolve(cfg, "annotations", "profile", profile, False)),
        show_annotations=bool(resolve(cfg, "annotations", "enabled", annotations, False)),
        enable_clean=bool(resolve(cfg, "clean", "enabled", clean, True)),
        basemap=str(resolve(cfg, "defaults", "basemap", basemap, "auto")),
        layers=_parse_layers(resolve(cfg, "defaults", "layers", layers, None)),
        bleed=bool(resolve(cfg, "defaults", "bleed", bleed, True)),
        cache_dir=resolve(cfg, "defaults", "cache_dir", cache_dir, None),
        use_osm=bool(resolve(cfg, "defaults", "osm", osm, True)),
    )

    files = _collect_gpx(inputs)
    directory = Path(resolve(cfg, "defaults", "out_dir", out_dir, Path("out")))
    dpi_value = int(resolve(cfg, "defaults", "dpi", dpi, 300))
    scale_value = float(resolve(cfg, "defaults", "png_scale", png_scale, 1.0))

    if separate:
        if out is not None:
            raise PipelineError("-o est incompatible avec --separate ; utilise --out-dir")
        for source, result in run_each(files, options):
            written = _write_outputs(
                result,
                directory / f"{_slugify(source.stem)}.svg",
                png=png,
                size=options.size,
                dpi=dpi_value,
                png_scale=scale_value,
            )
            console.print(f"[bold]{source.name}[/bold]")
            console.print(_report_table(result, written))
        return

    result = run(files, options)
    if out is not None:
        target = out
    else:
        stem = _slugify(options.title or default_title(result.tracks))
        target = directory / f"{stem}.svg"
    written = _write_outputs(
        result, target, png=png, size=options.size, dpi=dpi_value, png_scale=scale_value
    )
    console.print(_report_table(result, written))


# ---------------------------------------------------------------- info/thèmes


@app.command()
@click.argument("inputs", nargs=-1, required=True, type=PATH)
@click.option("--clean/--no-clean", default=True)
def info(inputs, clean) -> None:
    """Affiche les mesures d'un GPX sans rien produire."""
    from traceart.core.clean import clean_track
    from traceart.core.gpx import parse_gpx
    from traceart.core.stats import compute_stats

    table = Table(title="TraceArt · mesures")
    for column in ("fichier", "points", "segments", "distance", "D+", "durée", "élévation"):
        table.add_column(column)

    cwd = Path.cwd()
    for path in _collect_gpx(inputs):
        track = parse_gpx(path)
        if clean:
            track, _ = clean_track(track)
        stats = compute_stats(track)
        # round() avant formatage : `f"{-0.4:.0f}"` afficherait « -0 ».
        ele = (
            f"{round(stats.ele_min)} – {round(stats.ele_max)} m"
            if stats.has_elevation
            else "—"
        )
        # Chemin relatif : plusieurs dossiers contiennent un PARTIE_2.gpx.
        try:
            label = str(path.relative_to(cwd))
        except ValueError:
            label = path.name
        table.add_row(
            label,
            str(stats.n_points),
            str(stats.n_segments),
            f"{stats.distance_km:.1f} km",
            f"{stats.ascent_m:.0f} m" if stats.ascent_m else "—",
            stats.format_duration() or "—",
            ele,
        )
    console.print(table)


@app.command()
def themes() -> None:
    """Liste les thèmes disponibles."""
    table = Table(title="TraceArt · thèmes")
    table.add_column("nom")
    table.add_column("libellé")
    table.add_column("fond")
    table.add_column("trace")
    for name in available_themes():
        theme = load_theme(name)
        table.add_row(
            name,
            theme.label,
            theme.color("page.background"),
            ", ".join(theme.trace_colors()[:3]),
        )
    console.print(table)


@app.command()
def version() -> None:
    """Affiche la version."""
    console.print(f"traceart {__version__}")


# ----------------------------------------------------------------------- data


@app.group("data", help="Gestion du cache de données de fond (Natural Earth, domaine public).")
def data() -> None:
    pass


def _selected_datasets(scales: str | None, layers: str | None) -> list:
    wanted_scales = (
        [s.strip() for s in scales.split(",") if s.strip()] if scales else None
    )
    wanted_layers = _parse_layers(layers) if layers else None
    return [
        dataset
        for dataset in CATALOG
        if (wanted_scales is None or dataset.scale in wanted_scales)
        and (wanted_layers is None or dataset.layer in wanted_layers)
    ]


@data.command("fetch")
@click.option("--scale", help="10m, 50m, 110m (défaut : tous).")
@click.option("--layers", help="Couches à télécharger (défaut : toutes).")
@click.option("--force", is_flag=True, help="Retélécharge même si présent.")
@click.option("--prune", is_flag=True, help="Supprime les zips après conversion.")
@click.option("--cache-dir", type=PATH)
def data_fetch(scale, layers, force, prune, cache_dir) -> None:
    """Télécharge les données de fond dans le cache local.

    Après cette étape, TraceArt fonctionne entièrement hors ligne.
    """
    store = Store(cache_dir)
    datasets = _selected_datasets(scale, layers)
    if not datasets:
        raise PipelineError("aucun jeu ne correspond à --scale / --layers")

    console.print(f"{len(datasets)} jeu(x) Natural Earth → [dim]{store.root}[/dim]")

    def progress(dataset, state: str) -> None:
        if state == "download":
            console.print(f"  [dim]↓[/dim] {dataset.name}")
        elif state == "skip":
            console.print(f"  [dim]· {dataset.name} (déjà présent)[/dim]")

    entries = store.fetch(datasets, force=force, on_progress=progress)
    if prune:
        freed = store.prune_raw()
        console.print(f"  [dim]zips supprimés : {freed / 1e6:.0f} Mo[/dim]")

    total = sum(entries[d.name].features for d in datasets if d.name in entries)
    console.print(
        f"[green]✓[/green] {len(datasets)} jeu(x), {total} entités, "
        f"cache {store.total_size() / 1e6:.0f} Mo"
    )


@data.command("status")
@click.option("--cache-dir", type=PATH)
def data_status(cache_dir) -> None:
    """Montre ce que contient le cache de données de fond."""
    store = Store(cache_dir)
    table = Table(title=f"TraceArt · cache ({store.root})")
    for column in ("couche", "jeu", "résolution", "état", "entités"):
        table.add_column(column)
    for dataset, present, features in store.status():
        table.add_row(
            dataset.layer,
            dataset.name,
            dataset.scale,
            "[green]présent[/green]" if present else "[dim]absent[/dim]",
            str(features) if features else "—",
        )
    console.print(table)
    console.print(f"taille totale : {store.total_size() / 1e6:.1f} Mo")


@data.command("layers")
def data_layers() -> None:
    """Liste les couches de fond et les jeux qui les alimentent."""
    table = Table(title="TraceArt · couches de fond")
    table.add_column("couche")
    table.add_column("par défaut")
    table.add_column("jeux Natural Earth")
    for layer in AVAILABLE_LAYERS:
        names = sorted(
            d.name.removeprefix("ne_10m_").removeprefix("ne_50m_")
            for d in datasets_for(layer, "10m")
        )
        table.add_row(
            layer,
            "oui" if layer in DEFAULT_LAYERS else "non",
            ", ".join(names),
        )
    console.print(table)


# ------------------------------------------------------------------- data osm


@data.group("osm", help="Extraits OpenStreetMap grand échelle (Tier B).")
def osm() -> None:
    pass


def _human(size: float) -> str:
    return f"{size / 1e6:.0f} Mo" if size >= 1e6 else f"{size / 1e3:.0f} ko"


def _osm_store(cache_dir: Path | None) -> OsmStore:
    return OsmStore(cache_dir or default_cache_dir())


def _resolve_bbox(bbox, around) -> tuple[float, float, float, float] | None:
    """`--bbox` explicite, ou emprise déduite de `--around` (GPX)."""
    if bbox:
        parts = [p.strip() for p in bbox.replace(";", ",").split(",")]
        if len(parts) != 4:
            raise PipelineError(
                f"--bbox attend 4 valeurs min_lon,min_lat,max_lon,max_lat (reçu {bbox!r})"
            )
        try:
            values = tuple(float(p) for p in parts)
        except ValueError as exc:
            raise PipelineError(f"--bbox invalide : {bbox!r}") from exc
        if values[0] >= values[2] or values[1] >= values[3]:
            raise PipelineError(f"--bbox vide ou inversé : {bbox!r}")
        return values
    if around:
        from traceart.core.gpx import parse_many
        from traceart.render.layout import combined_bounds

        tracks = parse_many([str(p) for p in _collect_gpx(around)])
        return OsmStore.bbox_around(combined_bounds(tracks))
    return None


def _osm_import(
    store: OsmStore,
    path: Path,
    *,
    slug: str | None,
    sha256: str,
    bbox: tuple[float, float, float, float] | None = None,
) -> None:
    console.print(f"import [bold]{path.name}[/bold] — la lecture du .pbf est l'étape longue")
    if bbox:
        console.print(
            f"  [dim]découpe {bbox[0]:.2f},{bbox[1]:.2f} → {bbox[2]:.2f},{bbox[3]:.2f}[/dim]"
        )
    labels = {
        "lines": "cours d'eau et routes",
        "multipolygons": "plans d'eau et limites",
        "points": "labels de villes",
    }
    region = store.import_extract(
        path,
        slug=slug,
        sha256=sha256,
        bbox=bbox,
        on_progress=lambda stage: console.print(f"  [dim]· {labels.get(stage, stage)}[/dim]"),
    )
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="dim")
    table.add_column()
    table.add_row("région", region.slug)
    table.add_row(
        "emprise",
        f"{region.bounds[0]:.2f}, {region.bounds[1]:.2f} → "
        f"{region.bounds[2]:.2f}, {region.bounds[3]:.2f}",
    )
    for layer in sorted(region.counts):
        table.add_row(layer, str(region.counts[layer]))
    table.add_row("cache OSM", _human(store.total_size()))
    console.print(table)


@osm.command("fetch")
@click.argument("region")
@click.option("--bbox", help="Découpe à l'import : min_lon,min_lat,max_lon,max_lat")
@click.option(
    "--around",
    multiple=True,
    type=PATH,
    help="Découpe autour de ce ou ces GPX (marge 25 %).",
)
@click.option("--keep-pbf", is_flag=True, help="Conserver le .osm.pbf après import.")
@click.option("--cache-dir", type=PATH)
def osm_fetch(region, bbox, around, keep_pbf, cache_dir) -> None:
    """Télécharge un extrait Geofabrik puis l'importe.

    Préfère une sous-région à un pays entier : `france` pèse plus de 4 Go
    et son import est long, alors qu'une région administrative suffit
    largement pour une trace de rando.
    """
    store = _osm_store(cache_dir)
    url = geofabrik_url(region)
    console.print(f"↓ [dim]{url}[/dim]")

    state = {"last": -1}

    def progress(seen: int, total: int) -> None:
        if not total:
            return
        percent = int(seen * 100 / total)
        if percent >= state["last"] + 10:
            state["last"] = percent
            console.print(f"  [dim]{percent:>3} % · {_human(seen)} / {_human(total)}[/dim]")

    clip = _resolve_bbox(bbox, list(around) or None)
    pbf, digest = store.download(region, on_progress=progress)
    console.print(f"  [dim]sha256 {digest[:16]}…[/dim]")
    _osm_import(store, pbf, slug=slugify_region(region), sha256=digest, bbox=clip)
    if not keep_pbf:
        pbf.unlink(missing_ok=True)
        console.print("  [dim].osm.pbf supprimé (--keep-pbf pour le garder)[/dim]")


@osm.command("import")
@click.argument("path", type=EXISTING_PATH)
@click.option("--name", help="Nom de la région. Défaut : nom du fichier.")
@click.option("--bbox", help="Découpe à l'import : min_lon,min_lat,max_lon,max_lat")
@click.option(
    "--around",
    multiple=True,
    type=PATH,
    help="Découpe autour de ce ou ces GPX (marge 25 %).",
)
@click.option("--cache-dir", type=PATH)
def osm_import(path, name, bbox, around, cache_dir) -> None:
    """Importe un extrait OSM déjà téléchargé.

    Au-delà de quelques centaines de Mo, découpe avec `--bbox` ou
    `--around trace.gpx` : les entités retenues arrivent sinon en une
    seule fois en mémoire.
    """
    store = _osm_store(cache_dir)
    _osm_import(
        store,
        path,
        slug=slugify_region(name) if name else None,
        sha256="",
        bbox=_resolve_bbox(bbox, list(around) or None),
    )


@osm.command("list")
@click.option("--cache-dir", type=PATH)
def osm_list(cache_dir) -> None:
    """Liste les extraits OSM importés."""
    store = _osm_store(cache_dir)
    regions = store.regions()
    if not regions:
        console.print(
            "aucun extrait OSM importé — "
            "[dim]traceart data osm fetch europe/france/languedoc-roussillon[/dim]"
        )
        return
    table = Table(title=f"TraceArt · extraits OSM ({store.root})")
    for column in ("région", "emprise", "entités", "couches", "importé"):
        table.add_column(column)
    for region in regions:
        table.add_row(
            region.slug,
            f"{region.bounds[0]:.2f},{region.bounds[1]:.2f} → "
            f"{region.bounds[2]:.2f},{region.bounds[3]:.2f}",
            str(region.features),
            ", ".join(sorted(region.counts)),
            region.imported[:10],
        )
    console.print(table)
    console.print(f"taille : {_human(store.total_size())}")


@osm.command("remove")
@click.argument("name")
@click.option("--cache-dir", type=PATH)
def osm_remove(name, cache_dir) -> None:
    """Supprime un extrait OSM importé."""
    store = _osm_store(cache_dir)
    slug = slugify_region(name)
    if not store.remove(slug):
        raise OsmError(f"région inconnue : {slug}")
    console.print(f"[green]✓[/green] {slug} supprimé")


def main() -> None:
    """Point d'entrée : rend `render` implicite devant un chemin."""
    argv = sys.argv[1:]
    if argv and argv[0] not in COMMANDS and not argv[0].startswith("-"):
        sys.argv.insert(1, "render")
    try:
        app()
    except (GpxError, PipelineError, ThemeError, PngUnavailable, OsmError, ValueError) as exc:
        err_console.print(f"[red]erreur[/red] {exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
