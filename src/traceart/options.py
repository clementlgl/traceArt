"""Construction d'`Options` à partir d'une configuration et d'overrides.

C'est le point qui manquait pour qu'une seconde façade (l'interface web)
n'ait pas à reproduire le mapping section/clé TOML → champ d'`Options` :
avant ce module, ces ~20 correspondances étaient codées en dur dans la
fonction `render` du CLI. Toute nouvelle façade les aurait dupliquées, au
risque de diverger silencieusement de l'une ou de l'autre.

Ce module importe `pipeline` (pour `Options`, `parse_aspect`,
`DEFAULT_TOLERANCE`), `core.clean.CleanConfig`, `layers` et `config`. Il
n'importe ni `tomllib` ni aucun framework web : il reçoit un `Mapping`
déjà parsé et un dict d'overrides déjà résolus par l'appelant, quel qu'il
soit.

`SPECS` est la table déclarative, unique source de vérité — c'est elle
que l'invariant de tests (`tests/test_invariants.py`) confronte à
`fields(Options)` pour empêcher qu'un champ ajouté à `Options` reste
non déclaré.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from traceart.config import ConfigError, resolve
from traceart.core.clean import CleanConfig
from traceart.errors import TraceArtError
from traceart.layers import AVAILABLE_LAYERS, DEFAULT_LAYERS
from traceart.pipeline import DEFAULT_TOLERANCE, Options, PipelineError, parse_aspect


def _identity(value: Any) -> Any:
    return value


def parse_layers(raw: object) -> tuple[str, ...]:
    """`"water,rivers"` ou `["water", "rivers"]` → tuple validé.

    `None` (couche non précisée) retombe sur `DEFAULT_LAYERS` ; `"none"`
    (explicitement demandé) donne un tuple vide.
    """
    if raw is None:
        return DEFAULT_LAYERS
    if isinstance(raw, str):
        names = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        names = [str(part).strip() for part in raw]
    else:
        raise PipelineError(f"valeur de couches invalide : {raw!r}")
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


@dataclass(frozen=True, slots=True)
class OptionSpec:
    """Une ligne du mapping config → champ, avec assez d'information pour
    qu'une interface puisse aussi en dériver un formulaire."""

    field: str
    target: Literal["options", "clean", "output"]
    section: str
    key: str
    default: Any
    coerce: Callable[[Any], Any] = _identity
    # Exposée dans un formulaire (l'essentiel visuel), avec un libellé.
    ui: bool = False
    label: str = ""


@dataclass(frozen=True, slots=True)
class OutputOptions:
    """Réglages de sortie, communs au CLI et au web mais absents d'`Options` :
    le cœur produit un SVG en mémoire, il ne sait pas où l'écrire ni à
    quelle résolution le rasteriser."""

    out_dir: Path = Path("out")
    dpi: int = 300
    png_scale: float = 1.0


# Champs d'`Options` volontairement hors de cette table : ils ne sont
# jamais lus depuis la configuration (comportement inchangé de longue
# date), ou construits séparément (`clean`, assemblé depuis les specs
# target="clean" par `build_clean`).
ADAPTER_ONLY = frozenset({"clean", "title", "subtitle"})

SPECS: tuple[OptionSpec, ...] = (
    OptionSpec("theme", "options", "defaults", "theme", "light", str,
               ui=True, label="Thème"),
    OptionSpec("projection", "options", "defaults", "projection", "auto", str),
    OptionSpec("tolerance", "options", "defaults", "tolerance", DEFAULT_TOLERANCE, float),
    # Pas de cast : le CLI ne l'a jamais fait (le type Click --margin=float
    # s'en charge côté CLI ; une config TOML fautive remontait déjà telle
    # quelle). Comportement préservé à l'identique.
    OptionSpec("margin", "options", "defaults", "margin", None),
    OptionSpec("aspect", "options", "defaults", "aspect", None, parse_aspect,
               ui=True, label="Format / ratio"),
    OptionSpec("size", "options", "defaults", "size", "2000px", str),
    OptionSpec("show_stats", "options", "annotations", "stats", True, bool,
               ui=True, label="Mesures dans le bandeau"),
    OptionSpec("show_profile", "options", "annotations", "profile", False, bool,
               ui=True, label="Profil d'élévation"),
    OptionSpec("show_annotations", "options", "annotations", "enabled", False, bool,
               ui=True, label="Bandeau (titre, date, mesures)"),
    OptionSpec("enable_clean", "options", "clean", "enabled", True, bool),
    OptionSpec("basemap", "options", "defaults", "basemap", "auto", str,
               ui=True, label="Fond de carte"),
    OptionSpec("layers", "options", "defaults", "layers", None, parse_layers,
               ui=True, label="Couches de fond"),
    OptionSpec("bleed", "options", "defaults", "bleed", True, bool),
    # Idem margin : jamais casté en Path à ce site, les deux stores font
    # eux-mêmes Path(root).
    OptionSpec("cache_dir", "options", "defaults", "cache_dir", None),
    OptionSpec("use_osm", "options", "defaults", "osm", True, bool),
    OptionSpec("max_speed_kmh", "clean", "clean", "max_speed_kmh", 300.0, float),
    OptionSpec("pause_radius_m", "clean", "clean", "pause_radius_m", 12.0, float),
    OptionSpec("out_dir", "output", "defaults", "out_dir", Path("out"), Path),
    OptionSpec("dpi", "output", "defaults", "dpi", 300, int),
    OptionSpec("png_scale", "output", "defaults", "png_scale", 1.0, float),
)


def _resolve_spec(
    config: Mapping[str, Any], overrides: Mapping[str, Any], spec: OptionSpec
) -> Any:
    raw = resolve(dict(config), spec.section, spec.key, overrides.get(spec.field), spec.default)
    try:
        return spec.coerce(raw)
    except TraceArtError:
        # Déjà un message utile et un type de projet (parse_aspect,
        # parse_layers) : ne pas le masquer sous un second emballage.
        raise
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"[{spec.section}] {spec.key} : {exc}") from exc


def _collect(
    config: Mapping[str, Any] | None, overrides: Mapping[str, Any] | None, target: str
) -> dict[str, Any]:
    cfg = config or {}
    ov = overrides or {}
    return {spec.field: _resolve_spec(cfg, ov, spec) for spec in SPECS if spec.target == target}


def build_clean(
    config: Mapping[str, Any] | None = None, overrides: Mapping[str, Any] | None = None
) -> CleanConfig:
    return CleanConfig(**_collect(config, overrides, "clean"))


def build_output(
    config: Mapping[str, Any] | None = None, overrides: Mapping[str, Any] | None = None
) -> OutputOptions:
    return OutputOptions(**_collect(config, overrides, "output"))


def build_options(
    config: Mapping[str, Any] | None = None, overrides: Mapping[str, Any] | None = None
) -> Options:
    """Construit une `Options` complète : override explicite > config > défaut.

    `overrides` est un mapping `{nom_de_champ: valeur | None}` — le CLI y
    place ses paramètres Click, le web y placera son formulaire parsé.
    Ni l'un ni l'autre n'a besoin de connaître les noms de sections TOML.
    `title` et `subtitle` ne sont jamais lus depuis la config : ils
    viennent directement de l'override, ou valent `None`.
    """
    ov = overrides or {}
    return Options(
        clean=build_clean(config, overrides),
        title=ov.get("title"),
        subtitle=ov.get("subtitle"),
        **_collect(config, overrides, "options"),
    )


def ui_specs() -> tuple[OptionSpec, ...]:
    """Sous-ensemble de `SPECS` destiné à un formulaire : l'essentiel
    visuel, chacune munie d'un libellé prêt à afficher."""
    return tuple(s for s in SPECS if s.ui)
