"""Thèmes de style, en TOML.

Un thème est un fichier TOML fusionné par-dessus le thème de base
(`light`). Un thème utilisateur peut donc ne redéfinir que trois clés :
tout le reste est hérité. Les valeurs numériques sont exprimées en unités
de viewBox, dont le grand côté vaut `page.long_edge` — elles ont donc le
même rendu quelle que soit l'étendue géographique de la trace.
"""

from __future__ import annotations

import tomllib
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from traceart.errors import UserError
from traceart.layers import LAYER_LABELS, LAYER_ORDER

__all__ = ["LABEL_STYLES", "LAYER_LABELS", "LAYER_ORDER", "Theme", "load_theme"]

THEMES_DIR = Path(__file__).resolve().parent.parent / "themes"
BASE_THEME = "light"


# Style de texte par nature de label.
LABEL_STYLES = {"city": "labels", "country": "countries", "park": "parks"}



class ThemeError(UserError):
    """Thème introuvable ou TOML invalide."""


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = deepcopy(value)
    return out


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ThemeError(f"{path.name} : TOML invalide ({exc})") from exc
    except OSError as exc:
        raise ThemeError(f"{path} : lecture impossible ({exc})") from exc


@dataclass(frozen=True, slots=True)
class Theme:
    """Thème résolu (base + surcharges), interrogeable par chemin pointé."""

    name: str
    data: dict[str, Any]
    source: Path | None = None

    @property
    def label(self) -> str:
        return str(self.get("meta.label", self.name))

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def num(self, path: str, default: float = 0.0) -> float:
        value = self.get(path, default)
        return float(value) if isinstance(value, (int, float)) else default

    def color(self, path: str, default: str = "none") -> str:
        value = self.get(path, default)
        if not isinstance(value, str):
            return default
        # `auto` = couleur de fond de page : sert au halo sous la trace.
        if value == "auto":
            return str(self.get("page.background", default))
        return value

    def layer(self, name: str) -> dict[str, Any]:
        node = self.get(f"layers.{name}", {})
        return node if isinstance(node, dict) else {}

    def trace_colors(self) -> list[str]:
        colors = self.get("trace.colors", [])
        if isinstance(colors, str):
            return [colors]
        if isinstance(colors, list) and colors:
            return [str(c) for c in colors]
        return ["#d94f3d"]

    def trace_color(self, index: int) -> str:
        """Couleur de la n-ième trace, en boucle sur la palette."""
        colors = self.trace_colors()
        return colors[index % len(colors)]

    def with_trace_color(self, color: str) -> Theme:
        """Thème dérivé, palette de trace réduite à une seule couleur.

        Sert la personnalisation utilisateur (l'interface web propose un
        sélecteur de couleur) sans toucher au thème de base : toutes les
        autres clés (fond, couches, typographie) restent celles du thème
        choisi. Une seule couleur plutôt qu'une palette : c'est le cas
        d'usage — « je veux MA trace dans telle couleur » — et plusieurs
        traces rendues ensemble prendraient sinon toutes la même teinte,
        ce qui reste le compromis attendu d'un réglage volontairement
        simple.
        """
        merged = _deep_merge(self.data, {"trace": {"colors": [color]}})
        return replace(self, data=merged)


def available_themes() -> list[str]:
    """Noms des thèmes livrés, triés."""
    return sorted(p.stem for p in THEMES_DIR.glob("*.toml"))


def load_theme(name_or_path: str = BASE_THEME) -> Theme:
    """Charge un thème par nom livré ou par chemin de fichier TOML.

    Le résultat est toujours fusionné par-dessus `light`, y compris pour
    un thème utilisateur partiel.
    """
    base = _read_toml(THEMES_DIR / f"{BASE_THEME}.toml")

    candidate = Path(name_or_path)
    if candidate.suffix == ".toml":
        if not candidate.is_file():
            raise ThemeError(f"thème introuvable : {candidate}")
        override = _read_toml(candidate)
        name = str(override.get("meta", {}).get("name", candidate.stem))
        return Theme(name=name, data=_deep_merge(base, override), source=candidate)

    if name_or_path == BASE_THEME:
        return Theme(name=BASE_THEME, data=base, source=THEMES_DIR / f"{BASE_THEME}.toml")

    path = THEMES_DIR / f"{name_or_path}.toml"
    if not path.is_file():
        raise ThemeError(
            f"thème inconnu : {name_or_path!r} (disponibles : {', '.join(available_themes())})"
        )
    return Theme(
        name=name_or_path, data=_deep_merge(base, _read_toml(path)), source=path
    )
