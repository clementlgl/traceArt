"""Fichier de configuration `traceart.toml`.

Précédence : options CLI > fichier de config > valeurs par défaut du code.
La recherche s'arrête au premier fichier trouvé :

1. le chemin donné à `--config` ;
2. `./traceart.toml` (config par projet, versionnable) ;
3. `~/.config/traceart/config.toml` (préférences utilisateur).
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

CONFIG_NAME = "traceart.toml"
USER_CONFIG = Path.home() / ".config" / "traceart" / "config.toml"

# Sections reconnues ; toute autre clé de premier niveau est signalée.
SECTIONS = ("defaults", "clean", "annotations")

# Clés reconnues dans [annotations] : enabled (bandeau texte), stats, profile.


class ConfigError(ValueError):
    """Fichier de config introuvable ou invalide."""


def find_config(explicit: Path | None = None, cwd: Path | None = None) -> Path | None:
    if explicit is not None:
        if not explicit.is_file():
            raise ConfigError(f"config introuvable : {explicit}")
        return explicit
    local = (cwd or Path.cwd()) / CONFIG_NAME
    if local.is_file():
        return local
    if USER_CONFIG.is_file():
        return USER_CONFIG
    return None


def load_config(path: Path | None) -> dict[str, Any]:
    """Charge et valide la config. Renvoie un dict vide si aucun fichier."""
    if path is None:
        return {}
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} : TOML invalide ({exc})") from exc
    except OSError as exc:
        raise ConfigError(f"{path} : lecture impossible ({exc})") from exc

    unknown = [k for k in data if k not in SECTIONS]
    if unknown:
        raise ConfigError(
            f"{path} : section(s) inconnue(s) {unknown} (attendu : {', '.join(SECTIONS)})"
        )
    return data


def resolve(
    config: dict[str, Any],
    section: str,
    key: str,
    cli_value: Any,
    default: Any,
) -> Any:
    """Applique la précédence CLI > config > défaut pour une option."""
    if cli_value is not None:
        return cli_value
    value = config.get(section, {}).get(key)
    return default if value is None else value
